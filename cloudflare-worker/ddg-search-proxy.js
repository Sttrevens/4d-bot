/**
 * Cloudflare Worker: DuckDuckGo 搜索代理
 *
 * 国内服务器无法直连 DuckDuckGo，通过此 Worker 在海外执行搜索并返回结果。
 * 与 gemini-proxy 配合使用，不需要服务器上有梯子。
 *
 * 部署步骤：
 * 1. 登录 https://dash.cloudflare.com → Workers & Pages → Create
 * 2. 点 "Create Worker"，名称填 ddg-search-proxy
 * 3. 把这段代码粘贴进去，点 "Deploy"
 * 4. （推荐）在 Worker Settings > Variables 中添加 ACCESS_TOKEN，防止被滥用
 * 5. 复制部署后的 URL（形如 https://ddg-search-proxy.xxx.workers.dev）
 * 6. 在服务器 .env 文件中设置：
 *    DDG_SEARCH_PROXY_URL=https://ddg-search-proxy.xxx.workers.dev
 *    DDG_SEARCH_PROXY_TOKEN=<你设置的 ACCESS_TOKEN>  （如果配了 TOKEN）
 *
 * 调用方式：
 *   GET /search?q=关键词&max_results=5
 *   返回 JSON: { results: [{ title, body, href }, ...] }
 */

const DDG_URL = "https://html.duckduckgo.com/html/";

export default {
  async fetch(request, env) {
    // --- 访问控制 ---
    if (env.ACCESS_TOKEN) {
      const token =
        request.headers.get("X-Proxy-Token") ||
        new URL(request.url).searchParams.get("token");
      if (token !== env.ACCESS_TOKEN) {
        return jsonResponse({ error: "Unauthorized" }, 403);
      }
    }

    // --- CORS preflight ---
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "X-Proxy-Token",
        },
      });
    }

    const url = new URL(request.url);

    if (url.pathname !== "/search") {
      return jsonResponse(
        { error: "Not found. Use GET /search?q=keyword" },
        404,
      );
    }

    const query = url.searchParams.get("q");
    if (!query) {
      return jsonResponse({ error: "Missing 'q' parameter" }, 400);
    }

    const maxResults = Math.min(
      parseInt(url.searchParams.get("max_results") || "5", 10),
      20,
    );

    try {
      const results = await duckduckgoSearch(query, maxResults);
      return jsonResponse({ results });
    } catch (err) {
      return jsonResponse({ error: `Search failed: ${err.message}` }, 502);
    }
  },
};

/**
 * 通过 DuckDuckGo HTML 接口搜索并解析结果。
 * CF Worker 不能用 npm 包，所以手动解析 HTML。
 */
async function duckduckgoSearch(query, maxResults) {
  // DuckDuckGo HTML 搜索（比 API 更稳定）
  const formData = new URLSearchParams();
  formData.set("q", query);
  formData.set("kl", ""); // 不限区域

  const response = await fetch(DDG_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    },
    body: formData.toString(),
  });

  if (!response.ok) {
    throw new Error(`DuckDuckGo returned ${response.status}`);
  }

  const html = await response.text();
  return parseResults(html, maxResults);
}

/**
 * 从 DuckDuckGo HTML 页面提取搜索结果。
 * HTML 结构：每个结果是一个 class="result" 的 div，
 * 内含 class="result__a" 的链接和 class="result__snippet" 的摘要。
 */
function parseResults(html, maxResults) {
  const results = [];

  // 匹配每个搜索结果块
  const resultBlocks = html.match(
    /<div[^>]*class="[^"]*result[^"]*"[^>]*>[\s\S]*?<\/div>\s*<\/div>/gi,
  );
  if (!resultBlocks) return results;

  for (const block of resultBlocks) {
    if (results.length >= maxResults) break;

    // 提取标题和链接
    const linkMatch = block.match(
      /<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/i,
    );
    if (!linkMatch) continue;

    let href = linkMatch[1];
    const title = stripTags(linkMatch[2]).trim();

    // DuckDuckGo 的链接可能是重定向 URL，提取真实地址
    const uddgMatch = href.match(/[?&]uddg=([^&]+)/);
    if (uddgMatch) {
      href = decodeURIComponent(uddgMatch[1]);
    }

    // 提取摘要
    const snippetMatch = block.match(
      /<a[^>]*class="result__snippet"[^>]*>([\s\S]*?)<\/a>/i,
    );
    const body = snippetMatch ? stripTags(snippetMatch[1]).trim() : "";

    if (title && href) {
      results.push({ title, body, href });
    }
  }

  return results;
}

/** 去除 HTML 标签 */
function stripTags(html) {
  return html
    .replace(/<[^>]*>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&#39;/g, "'");
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
