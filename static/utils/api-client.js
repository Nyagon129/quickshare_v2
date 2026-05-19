/**
 * QuickShare API 客户端 (服务器中转版)
 * 封装所有与后端交互的接口
 */

// 动态获取服务器地址和端口（从当前页面URL获取）
function getApiBase() {
  // 如果已经设置了全局 CONFIG，使用它
  if (typeof window !== 'undefined' && window.CONFIG && window.CONFIG.API_BASE) {
    return window.CONFIG.API_BASE;
  }
  
  // 否则从当前页面URL动态获取
  const protocol = window.location.protocol;
  const hostname = window.location.hostname;
  const port = window.location.port;
  
  // 构建 API 基础 URL
  let apiBase = `${protocol}//${hostname}`;
  if (port) {
    apiBase += `:${port}`;
  }
  apiBase += '/api';
  
  return apiBase;
}

const API_BASE = getApiBase();
const API_VERSION = "/v1"; // 注意：API_BASE 已经包含了 /api，这里只需要 /v1

/**
 * 获取带 Authorization 头的请求头对象
 * @param {Object} customHeaders 自定义请求头
 * @returns {Object} 包含 Authorization 头的请求头对象
 */
export function getAuthHeaders(customHeaders = {}) {
  const token = localStorage.getItem("quickshare_token");
  const authHeader = token ? { Authorization: `Bearer ${token}` } : {};
  
  return {
    ...authHeader,
    ...customHeaders,
  };
}

/**
 * 统一的API请求函数
 * @param {string} method 请求方法
 * @param {string} endpoint API端点
 * @param {Object} body 请求体
 * @param {Object} customHeaders 自定义请求头
 */
export async function apiRequest(
  method,
  endpoint,
  body = null,
  customHeaders = {}
) {
  const url = `${API_BASE}${API_VERSION}${endpoint}`;

  // 使用辅助函数获取带 Authorization 头的请求头
  const headers = {
    Accept: "application/json",
    ...getAuthHeaders(customHeaders),
  };

  // GET 和 HEAD 请求不能有 body
  const methodsWithoutBody = ['GET', 'HEAD'];
  const canHaveBody = !methodsWithoutBody.includes(method.toUpperCase());

  // 如果body不是FormData且不为null，则添加Content-Type头（仅当请求可以包含body时）
  if (canHaveBody && body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const options = {
    method,
    headers,
  };

  // 添加请求体（仅当请求可以包含body时）
  if (canHaveBody && body) {
    options.body = body instanceof FormData ? body : JSON.stringify(body);
  }

  try {
    const response = await fetch(url, options);
    const data = await response.json();

    if (!response.ok) {
      // 后端返回格式: {code: 400, msg: "错误信息", data: {...}}
      const errorMsg = data.msg || data.message || `HTTP ${response.status}`;
      throw new Error(errorMsg);
    }

    // 返回完整的响应数据（包含 code, msg, data）
    return data;
  } catch (error) {
    console.error(`API请求失败 [${method} ${endpoint}]:`, error);
    throw error;
  }
}

/**
 * 取件码与文件元信息API
 */
export const CodeAPI = {
  // 创建取件码（开始上传流程）
  async createCode(fileInfo) {
    return await apiRequest("POST", "/codes", fileInfo);
  },

  // 验证取件码状态
  async getCodeStatus(code) {
    return await apiRequest("GET", `/codes/${code}/status`);
  },

  // 获取文件信息
  async getFileInfo(code) {
    return await apiRequest("GET", `/codes/${code}/file-info`);
  },

  // 记录完成一次领取
  async recordUsage(code) {
    return await apiRequest("POST", `/codes/${code}/usage`);
  },
};

/**
 * 文件分块传输API (服务器中转核心)
 */
export const TransferAPI = {
  // 【发送方】上传一个文件块（带认证）
  async uploadChunk(code, chunkIndex, chunkData, totalChunks) {
    const formData = new FormData();
    formData.append("chunk_index", chunkIndex);
    formData.append("chunk_data", new Blob([chunkData]), `chunk_${chunkIndex}`);
    formData.append("total_chunks", totalChunks);

    const url = `${API_BASE}${API_VERSION}/relay/codes/${code}/upload-chunk?chunk_index=${chunkIndex}`;
    const response = await fetch(url, {
      method: "POST",
      headers: getAuthHeaders(),
      body: formData,
    });
    return await response.json();
  },

  // 【发送方】通知服务器上传完成（带认证）
  async completeUpload(code, fileInfo) {
    const url = `${API_BASE}${API_VERSION}/relay/codes/${code}/upload-complete`;
    const response = await fetch(url, {
      method: "POST",
      headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(fileInfo),
    });
    return await response.json();
  },

  // 【接收方】获取一个文件块
  async downloadChunk(code, chunkIndex) {
    const response = await fetch(
      `${API_BASE}${API_VERSION}/relay/codes/${code}/download-chunk/${chunkIndex}`
    );
    if (!response.ok) throw new Error(`下载块失败: ${response.status}`);
    return await response.arrayBuffer();
  },

  // 【接收方】获取文件信息（总块数等）
  async getFileInfo(code) {
    return await apiRequest("GET", `/codes/${code}/file-info`);
  },
};

/**
 * 举报功能API
 */
export const ReportAPI = {
  async submitReport(code, reason) {
    return await apiRequest("POST", "/reports", { code, reason });
  },
};

/**
 * 服务器健康检查
 */
export const HealthAPI = {
  async check() {
    const response = await fetch(`${API_BASE}/health`);
    return response.ok;
  },
};

/**
 * 测试用：预置的测试取件码 (功能不变)
 */
export const TEST_CODES = {};
