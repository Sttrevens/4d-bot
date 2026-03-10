/**
 * Cloudflare Worker: Gemini API 反向代理
 *
 * 部署后，将 Worker URL 设为 GOOGLE_GEMINI_BASE_URL 环境变量即可。
 * 例如：GOOGLE_GEMINI_BASE_URL=https://gemini-proxy.your-subdomain.workers.dev
 *
 * 部署步骤：
 * 1. 登录 https://dash.cloudflare.com → Workers & Pages → Create
 * 2. 点 "Create Worker"，名称填 gemini-proxy
 * 3. 把这段代码粘贴进去，点 "Deploy"
 * 4. 复制部署后的 URL（形如 https://gemini-proxy.xxx.workers.dev）
 * 5. 在服务器 .env 文件中设置：
 *    GOOGLE_GEMINI_BASE_URL=https://gemini-proxy.xxx.workers.dev
 *
 * 安全说明：
 * - API Key 由客户端在请求头/参数中携带，Worker 只做透传
 * - Worker 本身不存储任何密钥
 * - 可选：设置 ACCESS_TOKEN 环境变量限制谁能用这个代理
 */

const GEMINI_API_HOST = "generativelanguage.googleapis.com";

export default {
  async fetch(request, env) {
    // 可选：访问控制 —— 在 Worker Settings > Variables 中设置 ACCESS_TOKEN
    if (env.ACCESS_TOKEN) {
      const token = request.headers.get("X-Proxy-Token");
      if (token !== env.ACCESS_TOKEN) {
        return new Response("Unauthorized", { status: 403 });
      }
    }

    // 构建目标 URL：保留原始路径和查询参数
    const url = new URL(request.url);
    url.hostname = GEMINI_API_HOST;
    url.protocol = "https:";
    url.port = "";

    // 复制请求头，移除 Cloudflare 特有头
    const headers = new Headers(request.headers);
    headers.delete("cf-connecting-ip");
    headers.delete("cf-ipcountry");
    headers.delete("cf-ray");
    headers.delete("cf-visitor");
    headers.delete("x-forwarded-proto");
    headers.delete("x-real-ip");
    headers.delete("X-Proxy-Token");

    // 转发请求
    const response = await fetch(url.toString(), {
      method: request.method,
      headers,
      body: request.body,
    });

    // 返回响应，添加 CORS 头（可选）
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  },
};
