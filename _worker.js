// Cloudflare Workers 自建TG API反代

var URL_PATH_REGEX = /^\/bot(?<bot_token>[^/]+)\/(?<api_method>[a-z]+)/i;

var src_default = {
  async fetch(request) {
    const { pathname: path, search } = new URL(request.url);

    let matchResult;
    let apiUrl;

    try {
      matchResult = path.match(URL_PATH_REGEX);
    } catch (e) {
      return new Response("Invalid URL", {
        status: 400,
        headers: { "content-type": "text/plain" }
      });
    }

    if (matchResult && matchResult.groups) {
      const { bot_token, api_method } = matchResult.groups;
      apiUrl = "https://api.telegram.org/bot" + bot_token + "/" + api_method + search;
    } else {
      return new Response("Invalid URL", {
        status: 400,
        headers: { "content-type": "text/plain" }
      });
    }

    if (request.method === "GET") {
      const response = await fetch(apiUrl, {
        method: "GET",
        headers: request.headers
      });
      const responseBody = await response.text();

      return new Response(responseBody, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers
      });
    } else if (request.method === "POST") {
      const response = await fetch(apiUrl, {
        method: request.method,
        headers: request.headers,
        body: request.body
      });
      const responseBody = await response.text();
      
      return new Response(responseBody, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers
      });
    } else {
      return new Response("Unsupported Request Method", {
        status: 400,
        headers: { "content-type": "text/plain" }
      });
    }
  }
};
export {
  src_default as default
};
