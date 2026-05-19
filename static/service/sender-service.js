/**
 * Sender服务 - 发送方服务
 * 使用服务器中转模式，支持端到端加密
 */

import { splitFileIntoChunks } from '../utils/file-utils.js';
import { generateSessionId } from '../utils/common-utils.js';
import { 
  generateEncryptionKey, 
  exportKeyToBase64,
  importKeyFromBase64,
  encryptChunk,
  calculateChunkHash,
  encryptKeyWithPickupCode,
  extractLookupCode
} from '../utils/encryption-utils.js';
import { getKeyFromCache, storeKeyInCache } from '../utils/key-cache.js';
import { getAuthHeaders } from '../utils/api-client.js';

class SenderService {
  constructor() {
    // 服务器中转相关
    this.pickupCode = null;              // 十二位取件码（前6位查找码+后6位密钥码）
    this.apiBase = '';                   // API服务器基础URL
    this.encryptionKey = null;           // 文件加密密钥（原始密钥，用于加密文件块，不是密钥码）
    this.encryptionKeyBase64 = null;     // 文件加密密钥Base64（用于浏览器缓存，已废弃，不再使用）
    
    // 回调函数
    this.onProgress = null;              // 发送进度
    this.onComplete = null;              // 发送完成
    this.onError = null;                 // 错误
    this.onValidating = null;            // 验证完整性
    
    // 文件传输相关
    this.currentFile = null;             // 当前待发送文件
    this.fileChunks = [];                // 分片后的文件块列表
    this.currentChunkIndex = 0;          // 已发送分片索引
    this.isSending = false;              // 是否正在发送中
  }

  /**
   * 初始化配置
   * @param {string} apiBase - API基础URL
   * @param {Object} callbacks - 回调函数对象
   */
  init(apiBase, callbacks = {}) {
    this.apiBase = apiBase;
    this.onProgress = callbacks.onProgress || null;
    this.onComplete = callbacks.onComplete || null;
    this.onError = callbacks.onError || null;
    this.onValidating = callbacks.onValidating || null;
  }

  // ========== 服务器中转模式 ==========
  
  /**
   * 通过服务器中转上传文件（使用端到端加密）
   * @param {string} pickupCode - 取件码
   * @param {File} file - 要上传的文件
   * @returns {Promise<string>} 返回加密密钥的Base64（用于分享给接收者）
   */
  async uploadFileViaRelay(pickupCode, file, fileHash = null, expireHours = 24 * 7) {
    if (!this.apiBase) {
      throw new Error('API基础URL未设置，请先调用init()方法');
    }

    if (!pickupCode || !/^[A-Z0-9]{12}$/.test(pickupCode)) {
      throw new Error('无效的取件码，必须是12位大写字母或数字');
    }

    if (!file) {
      throw new Error('文件不能为空');
    }

    if (this.isSending) {
      throw new Error('文件正在上传中，请等待完成');
    }

    // 保存完整的12位取件码（用于加密）
    this.pickupCode = pickupCode.toUpperCase();
    
    // 提取前6位查找码（只发送查找码到服务器，不暴露后6位密钥码）
    this.lookupCode = extractLookupCode(this.pickupCode);
    
    this.currentFile = file;
    this.isSending = true;
    this.currentChunkIndex = 0;

    try {
      // 1. 生成或获取文件加密密钥（原始密钥，用于加密文件块）
      // 注意：这是文件加密密钥，不是取件码的密钥码（后6位）
      if (fileHash) {
        const cachedKeyBase64 = getKeyFromCache(fileHash);
        if (cachedKeyBase64) {
          console.log('[Sender] 从缓存获取文件加密密钥（原始密钥）...');
          this.encryptionKey = await importKeyFromBase64(cachedKeyBase64);
          this.encryptionKeyBase64 = cachedKeyBase64;
          console.log('[Sender] ✓ 使用缓存的文件加密密钥（原始密钥）');
        } else {
          console.log('[Sender] 缓存中无密钥，生成新的文件加密密钥（原始密钥）...');
          this.encryptionKey = await generateEncryptionKey();
          this.encryptionKeyBase64 = await exportKeyToBase64(this.encryptionKey);
          console.log('[Sender] ✓ 文件加密密钥（原始密钥）已生成');
        }
      } else {
        console.log('[Sender] 无文件哈希，生成新的文件加密密钥（原始密钥）...');
        this.encryptionKey = await generateEncryptionKey();
        this.encryptionKeyBase64 = await exportKeyToBase64(this.encryptionKey);
        console.log('[Sender] ✓ 文件加密密钥（原始密钥）已生成');
      }

      // 2. 将文件分割成块
      console.log('[Sender] 分割文件为块...');
      const chunks = splitFileIntoChunks(file, 64 * 1024); // 64KB每块
      this.fileChunks = chunks;
      console.log(`[Sender] ✓ 文件已分割为 ${chunks.length} 个块`);

      // 2.5. 检查文件块是否已存在（复用旧文件块，避免重复上传）
      let existingChunks = [];
      let missingChunks = [];
      try {
        console.log('[Sender] 检查文件块是否已存在...');
        const checkUrl = `${this.apiBase}/relay/codes/${this.lookupCode}/check-chunks?total_chunks=${chunks.length}`;
        const checkResponse = await fetch(checkUrl, {
          headers: getAuthHeaders()
        });
        if (checkResponse.ok) {
          const checkResult = await checkResponse.json();
          if (checkResult.code === 200 && checkResult.data) {
            existingChunks = checkResult.data.existingChunks || [];
            missingChunks = checkResult.data.missingChunks || [];
            console.log(`[Sender] ✓ 文件块检查完成: ${existingChunks.length} 个已存在, ${missingChunks.length} 个需要上传`);
            if (existingChunks.length > 0) {
              console.log(`[Sender] 将复用 ${existingChunks.length} 个已存在的文件块，跳过上传`);
            }
          }
        }
      } catch (error) {
        console.warn('[Sender] 检查文件块失败，将上传所有块:', error);
        // 如果检查失败，上传所有块
        missingChunks = Array.from({ length: chunks.length }, (_, i) => i);
      }

      // 如果检查失败或无结果，上传所有块
      if (missingChunks.length === 0 && existingChunks.length === 0) {
        missingChunks = Array.from({ length: chunks.length }, (_, i) => i);
      }

      // 3. 并行上传缺失的文件块（跳过已存在的块）
      console.log('[Sender] 开始并行上传加密文件块...');
      const CONCURRENCY = 4;  // 同时上传的最大块数
      let uploadedCount = existingChunks.length;
      const totalChunks = chunks.length;

      // 先报告已复用块的进度
      if (existingChunks.length > 0) {
        const progress = (existingChunks.length / totalChunks) * 100;
        if (this.onProgress) {
          this.onProgress(progress, existingChunks.length, totalChunks);
        }
      }

      // 并行上传函数
      const uploadOneChunk = async (i) => {
        const chunk = chunks[i];
        const encryptedChunk = await encryptChunk(chunk, this.encryptionKey);
        const chunkHash = await calculateChunkHash(encryptedChunk);
        const formData = new FormData();
        formData.append('chunk_data', encryptedChunk, `chunk_${i}.encrypted`);
        formData.append('chunk_index', i.toString());
        const uploadUrl = `${this.apiBase}/relay/codes/${this.lookupCode}/upload-chunk?chunk_index=${i}`;

        const response = await fetch(uploadUrl, {
          method: 'POST',
          headers: getAuthHeaders(),
          body: formData
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.msg || `上传块 ${i} 失败: HTTP ${response.status}`);
        }

        const result = await response.json();
        if (result.code !== 200) {
          throw new Error(result.msg || `上传块 ${i} 失败`);
        }

        if (result.data.reused) {
          console.log(`[Sender] ✓ 块 ${i + 1}/${totalChunks} 已复用`);
        } else {
          if (result.data.chunkHash && result.data.chunkHash !== chunkHash) {
            throw new Error(`块 ${i} 哈希验证失败`);
          }
          console.log(`[Sender] ✓ 块 ${i + 1}/${totalChunks} 上传成功`);
        }
        return i;
      };

      // 分批并行执行
      for (let start = 0; start < missingChunks.length; start += CONCURRENCY) {
        const batch = missingChunks.slice(start, start + CONCURRENCY);
        await Promise.all(batch.map(i => uploadOneChunk(i)));

        uploadedCount += batch.length;
        this.currentChunkIndex = uploadedCount;
        const progress = (uploadedCount / totalChunks) * 100;
        if (this.onProgress) {
          this.onProgress(progress, uploadedCount, totalChunks);
        }
      }

      console.log('[Sender] ✓ 所有文件块上传完成');

      // 4. 使用取件码的密钥码（后6位）派生密钥，加密文件加密密钥（原始密钥），并存储到服务器
      // 注意：这里使用取件码的密钥码派生密钥来加密文件加密密钥，不是直接用密钥码
      console.log('[Sender] 使用取件码的密钥码派生密钥，加密文件加密密钥（原始密钥）...');
      const encryptedKey = await encryptKeyWithPickupCode(this.encryptionKey, this.pickupCode);
      console.log('[Sender] 文件加密密钥（原始密钥）已加密，准备存储到服务器...');
      await this.storeEncryptedKey(encryptedKey);
      console.log('[Sender] ✓ 加密后的文件加密密钥已存储到服务器');
      
      // 4.5. 存储密钥到浏览器缓存（以文件哈希为键）
      if (fileHash && this.encryptionKeyBase64) {
        // 使用取件码的过期时间（从参数传入）
        storeKeyInCache(fileHash, this.encryptionKeyBase64, expireHours);
        console.log(`[Sender] ✓ 加密密钥已存储到浏览器缓存（过期时间: ${expireHours}小时）`);
      }

      // 5. 通知服务器上传完成
      await this.notifyUploadComplete();

      // 6. 完成回调（不再需要返回密钥，用户只需分享取件码）
      if (this.onComplete) {
        this.onComplete();
      }

      this.isSending = false;
      // 不再返回密钥，用户只需分享取件码
      return null;
    } catch (error) {
      this.isSending = false;
      console.error('[Sender] 上传文件失败:', error);
      if (this.onError) {
        this.onError(error);
      }
      throw error;
    }
  }

  /**
   * 存储加密后的文件加密密钥到服务器
   * 
   * 注意：这里存储的是用取件码的密钥码派生密钥加密后的文件加密密钥（原始密钥）
   * 不是密钥码本身，也不是未加密的文件加密密钥
   * 
   * @param {string} encryptedKeyBase64 用取件码的密钥码派生密钥加密后的文件加密密钥（Base64编码）
   * @returns {Promise<void>}
   */
  async storeEncryptedKey(encryptedKeyBase64) {
    try {
      console.log(`[Sender] 正在存储加密密钥到服务器 (lookupCode: ${this.lookupCode})...`);
      const response = await fetch(
        `${this.apiBase}/relay/codes/${this.lookupCode}/store-encrypted-key`,
        {
          method: 'POST',
          headers: getAuthHeaders({
            'Content-Type': 'application/json'
          }),
          body: JSON.stringify({
            encryptedKey: encryptedKeyBase64
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        const errorMsg = errorData.msg || `存储加密密钥失败: HTTP ${response.status}`;
        console.error(`[Sender] ✗ 存储加密密钥失败: ${errorMsg}`, errorData);
        throw new Error(errorMsg);
      }

      const result = await response.json();
      if (result.code !== 200) {
        console.error(`[Sender] ✗ 存储加密密钥失败: ${result.msg}`, result);
        throw new Error(result.msg || '存储加密密钥失败');
      }

      console.log('[Sender] ✓ 加密密钥已存储到服务器');
    } catch (error) {
      console.error('[Sender] ✗ 存储加密密钥失败:', error);
      // 抛出错误，因为Receiver需要这个密钥才能解密文件
      throw error;
    }
  }

  /**
   * 通知服务器上传完成
   * @returns {Promise<void>}
   */
  async notifyUploadComplete() {
    try {
      // 通知开始验证完整性
      if (this.onValidating) {
        this.onValidating();
      }
      
      const response = await fetch(
        `${this.apiBase}/relay/codes/${this.lookupCode}/upload-complete`,
        {
          method: 'POST',
          headers: getAuthHeaders({
            'Content-Type': 'application/json'
          }),
          body: JSON.stringify({
            totalChunks: this.fileChunks.length,
            fileSize: this.currentFile.size,
            fileName: this.currentFile.name,
            mimeType: this.currentFile.type
          })
        }
      );

      if (!response.ok) {
        // 尝试解析错误响应
        let errorMsg = `验证完整性失败: HTTP ${response.status}`;
        try {
          const errorData = await response.json();
          if (errorData.msg) {
            errorMsg = errorData.msg;
          }
          // 如果是完整性验证失败，抛出错误让前端处理
          if (errorData.data && errorData.data.code === 'INCOMPLETE_UPLOAD') {
            throw new Error(errorMsg);
          }
        } catch (jsonError) {
          // JSON 解析失败，使用默认错误信息
        }
        throw new Error(errorMsg);
      }

      console.log('[Sender] ✓ 已通知服务器上传完成，验证通过');
    } catch (error) {
      console.error('[Sender] 验证完整性失败:', error);
      // 验证失败时抛出错误，让前端处理（清除验证状态、显示错误等）
      throw error;
    }
  }

  /**
   * 获取加密密钥（已废弃，不再需要）
   * @deprecated 现在用户只需分享取件码，密钥会自动从服务器获取
   * @returns {string|null} 加密密钥的Base64字符串
   */
  getEncryptionKey() {
    return null; // 不再返回密钥
  }

}

// 导出单例实例
export const senderService = new SenderService();

