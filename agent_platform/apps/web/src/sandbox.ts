// Adapted from agent-agui-generated-ui/frontend/src/sandbox.ts.
export type GenerativeContent = {
  initialHeight?: number;
  generating?: boolean;
  css?: string;
  cssComplete?: boolean;
  html?: string[];
  htmlComplete?: boolean;
  jsFunctions?: string;
  jsFunctionsComplete?: boolean;
  jsExpressions?: string[];
  jsExpressionsComplete?: boolean;
};

export type ArtifactFailure = {
  name: string;
  message: string;
  stage: "initialization" | "runtime";
};

type ArtifactStatusMessage = {
  type: "artifact-status";
  status: "ready" | "error";
  channel: string;
  error?: ArtifactFailure;
};

export const MAX_ARTIFACT_CHARS = 200_000;
export function parseGenerativeContent(value: unknown): GenerativeContent | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const c = value as Record<string, unknown>;
  for (const key of ["generating", "cssComplete", "htmlComplete", "jsFunctionsComplete", "jsExpressionsComplete"]) {
    if (c[key] !== undefined && typeof c[key] !== "boolean") return null;
  }
  if ((c.css !== undefined && typeof c.css !== "string") ||
    (c.jsFunctions !== undefined && typeof c.jsFunctions !== "string") ||
    (c.initialHeight !== undefined && (typeof c.initialHeight !== "number" || !Number.isFinite(c.initialHeight)))) return null;
  for (const key of ["html", "jsExpressions"]) {
    if (c[key] !== undefined && (!Array.isArray(c[key]) || !(c[key] as unknown[]).every(v => typeof v === "string"))) return null;
  }
  if (JSON.stringify(c).length > MAX_ARTIFACT_CHARS) return null;
  return c as GenerativeContent;
}

export function artifactPhase(content: GenerativeContent): "placeholder" | "preview" | "initialize" | "empty" {
  const hasHtml = !!content.html?.join("").trim();
  if (content.generating === false && !hasHtml) return "empty";
  const scriptsComplete = content.jsFunctionsComplete !== false && content.jsExpressionsComplete !== false;
  if (content.generating === false && content.htmlComplete === true && content.cssComplete === true && scriptsComplete && hasHtml) return "initialize";
  return content.cssComplete && hasHtml ? "preview" : "placeholder";
}

function safeCss(css: string): string {
  // External resources are also denied by CSP. Strip these constructs from the
  // submitted markup to keep preview/final resource behavior consistent.
  return css.replace(/@import\s+[^;]+;?/gi, "").replace(/url\s*\([^)]*\)/gi, "none")
    .replace(/expression\s*\([^)]*\)/gi, "").replace(/<\/style/gi, "<\\/style")
    // The iframe grows to its content. Using its own viewport for vh would
    // feed that new height back into the content (100vh + a footer grows forever).
    .replace(/(-?(?:\d*\.)?\d+)(?:dvh|svh|lvh|vh)\b/gi,
      (_, amount: string) => `calc(var(--artifact-viewport-height) * ${Number(amount) / 100})`);
}

const htmlNamespace = "http://www.w3.org/1999/xhtml";
const svgNamespace = "http://www.w3.org/2000/svg";
// Charts need inline geometry, text, gradients, and clipping. Exclude SVG's
// resource, animation, navigation, and embedded HTML capabilities explicitly.
const svgElements = new Set([
  "svg", "g", "defs", "path", "polyline", "polygon", "line", "rect", "circle", "ellipse",
  "text", "tspan", "title", "desc", "clippath", "lineargradient", "radialgradient", "stop",
  "pattern", "marker",
]);
const svgAttributes = new Set([
  "id", "class", "style", "role", "tabindex", "focusable", "viewbox", "preserveaspectratio",
  "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry",
  "d", "points", "pathlength", "transform", "fill", "fill-opacity", "fill-rule", "clip-rule",
  "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
  "stroke-dasharray", "stroke-dashoffset", "opacity", "color", "font-family", "font-size", "font-weight",
  "font-style", "text-anchor", "dominant-baseline", "alignment-baseline", "dx", "dy", "rotate",
  "textlength", "lengthadjust", "clip-path", "clippathunits", "gradientunits", "gradienttransform",
  "spreadmethod", "offset", "stop-color", "stop-opacity", "fx", "fy", "fr", "markerheight",
  "markerwidth", "markerunits", "orient", "refx", "refy", "marker-start", "marker-mid", "marker-end",
  "patternunits", "patterncontentunits", "patterntransform", "vector-effect", "display", "visibility",
  "overflow", "pointer-events",
]);
const localSvgReference = /^url\(\s*(['"]?)#[a-z_][\w:.-]*\1\s*\)$/i;

export function sanitizeHtml(html: string): string {
  // A DOMParser document is script-inert but may still fetch image/frame URLs.
  // First parse into template content, whose resource elements remain inert;
  // strip capabilities before the normalized DOMParser document sees them.
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script,style,link,meta,base,iframe,object,embed,form,input[type=file],math,audio,video,source,template").forEach(node => node.remove());
  template.content.querySelectorAll("*").forEach(element => {
    const isSvg = element.namespaceURI === svgNamespace;
    if (isSvg ? !svgElements.has(element.localName.toLowerCase()) : element.namespaceURI !== htmlNamespace) {
      element.remove();
      return;
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase();
      if (isSvg && ((!svgAttributes.has(name) && !name.startsWith("aria-") && !name.startsWith("data-")) ||
        (name !== "style" && /url\s*\(/i.test(attribute.value) && !localSvgReference.test(attribute.value)))) {
        element.removeAttribute(attribute.name);
      } else if (name.startsWith("on") || ["href", "xlink:href", "srcdoc", "srcset", "action", "formaction", "form", "target", "download", "ping", "nonce", "http-equiv", "integrity", "crossorigin"].includes(name)) {
        element.removeAttribute(attribute.name);
      } else if (name === "src") {
        if (element.tagName !== "IMG" || !/^data:image\/(?:png|jpe?g|webp|gif);base64,[a-z0-9+/=\s]+$/i.test(attribute.value)) element.removeAttribute(attribute.name);
      } else if (name === "style") {
        element.setAttribute("style", safeCss(attribute.value));
      }
    }
  });
  return new DOMParser().parseFromString(template.innerHTML, "text/html").body.innerHTML;
}

function scriptJson(value: unknown): string {
  // HTML's parser recognizes </script> even inside a JS string. Escaping every
  // < in JSON avoids a generated string closing the trusted bootstrap script.
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}

const csp = (nonce?: string) => `default-src 'none'; script-src ${nonce ? `'nonce-${nonce}'` : "'none'"}; script-src-elem ${nonce ? `'nonce-${nonce}'` : "'none'"}; script-src-attr 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'`;
// CSP policies intersect: require our nonce AND an inline script, so generated
// code cannot reuse the nonce to load an external script URL.
const inlineOnlyPolicy = '<meta http-equiv="Content-Security-Policy" content="script-src-elem \'unsafe-inline\'">';
const baseStyle = "html{color-scheme:light}body{margin:0;padding:0;font:14px system-ui,sans-serif;overflow-wrap:anywhere}*{box-sizing:border-box}:where(body>:not(script):not(style)){max-width:100%}";
// The embedded page owns its component spacing. Keep its outer canvas bare,
// center constrained page roots, and measure height independently of the frame.
// Target roots only so component shadows and internal layout remain intact.
const embeddedStyle = "html,body{margin:0!important;padding:0!important;width:auto!important;max-width:none!important;height:auto!important;min-height:0!important;max-height:none!important;background:transparent!important;box-shadow:none!important;filter:none!important}html{overflow:hidden!important}body{display:flow-root!important;overflow:visible!important}body>:not(script):not(style){margin-inline:auto!important;box-shadow:none!important;filter:none!important}";

function resizeBridge(channel: string): string {
  return `(() => {
    const channel = ${scriptJson(channel)};
    const setViewport = (height) => {
      document.documentElement.style.setProperty('--artifact-viewport-height', height + 'px', 'important');
    };
    setViewport(${Math.max(1, Math.round(window.innerHeight))});
    let queued = false;
    let lastHeight = 0;
    const schedule = () => {
      if (queued || !document?.body) return;
      queued = true;
      const nextFrame = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 0));
      nextFrame(() => {
        queued = false;
        const body = document?.body;
        if (!body) return;
        // documentElement.scrollHeight is floored by the viewport height and
        // would prevent a previously tall iframe from shrinking again.
        const height = Math.ceil(Math.max(1, body.getBoundingClientRect().height, body.scrollHeight));
        if (Number.isSafeInteger(height) && height !== lastHeight) {
          lastHeight = height;
          parent.postMessage({ channel, type: 'artifact-resize', height }, '*');
        }
      });
    };
    if (typeof ResizeObserver === 'function') new ResizeObserver(schedule).observe(document.body);
    new MutationObserver(schedule).observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
    window.addEventListener('resize', schedule);
    window.addEventListener('message', (event) => {
      const data = event.data;
      if (event.source !== parent || !data || typeof data !== 'object' || Array.isArray(data) ||
        Object.keys(data).length !== 3 || data.type !== 'artifact-viewport' || data.channel !== channel ||
        !Number.isSafeInteger(data.height) || data.height < 1) return;
      setViewport(data.height);
      schedule();
    });
    document.addEventListener('load', schedule, true);
    schedule();
  })();`;
}

export function previewDocument(content: GenerativeContent, channel = crypto.randomUUID().replaceAll("-", "")): string {
  // Only this trusted size bridge runs during preview. Generated JS is omitted.
  const updateBridge = `(() => {
    const channel = ${scriptJson(channel)};
    const bridge = document.currentScript;
    const style = document.querySelector('style');
    window.addEventListener('message', (event) => {
      const data = event.data;
      if (event.source !== parent || !data || typeof data !== 'object' || Array.isArray(data) ||
        Object.keys(data).length !== 4 || data.type !== 'artifact-preview' || data.channel !== channel ||
        typeof data.html !== 'string' || typeof data.css !== 'string') return;
      // The host sends sanitized markup and CSS; generated script fields never
      // cross this bridge. Keep the document/observers alive while replacing HTML.
      if (style.textContent !== data.css) style.textContent = data.css;
      const template = document.createElement('template');
      template.innerHTML = data.html;
      document.body.replaceChildren(template.content, bridge);
    });
  })();`;
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp(channel)}">${inlineOnlyPolicy}<style>${baseStyle}\n${safeCss(content.css ?? "")}\n${embeddedStyle}</style></head><body>${sanitizeHtml(content.html?.join("") ?? "")}<script nonce="${channel}">${resizeBridge(channel)}\n${updateBridge}</script></body></html>`;
}

export function previewUpdate(content: GenerativeContent, channel: string) {
  return { type: "artifact-preview", channel, html: sanitizeHtml(content.html?.join("") ?? ""),
    css: `${baseStyle}\n${safeCss(content.css ?? "")}\n${embeddedStyle}` };
}

export function finalDocument(content: GenerativeContent, channel: string): string {
  const payload = scriptJson({ functions: content.jsFunctions ?? "", expressions: content.jsExpressions ?? [] });
  const channelJson = scriptJson(channel);
  const bootstrap = `${resizeBridge(channel)}\n(() => {
    const channel = ${channelJson};
    let failed = false;
    let initialized = false;
    const report = (status, error) => parent.postMessage({ channel, type: 'artifact-status', status, ...(error ? {error} : {}) }, '*');
    const fail = (reason) => {
      if (failed) return;
      failed = true;
      let name = 'Error', message = '未知脚本异常';
      const clean = (value, limit) => String(value).replace(/[\\u0000-\\u001f\\u007f]/g, ' ').trim().slice(0, limit);
      try {
        name = clean(reason && reason.name || 'Error', 40) || name;
        message = clean(reason && reason.message || reason || message, 240) || message;
      } catch {}
      report('error', {name, message, stage: initialized ? 'runtime' : 'initialization'});
    };
    window.addEventListener('error', (event) => fail(event.error || {message: event.message}));
    window.addEventListener('unhandledrejection', (event) => fail(event.reason));
    document.addEventListener('click', (event) => {
      if (event.target instanceof Element && event.target.closest('a,area')) event.preventDefault();
    }, true);
    document.addEventListener('submit', (event) => event.preventDefault(), true);
    const payload = ${payload};
    const script = document.createElement('script');
    script.nonce = channel;
    const expressions = payload.expressions.join('\\n;\\n');
    script.textContent = '(async () => {\\n' + payload.functions + '\\n;\\n' + expressions + '\\n})().then(() => window.dispatchEvent(new Event("artifact-initialized"))).catch((error) => window.dispatchEvent(new CustomEvent("artifact-failed", {detail: error})));';
    window.addEventListener('artifact-failed', (event) => fail(event.detail), { once: true });
    window.addEventListener('artifact-initialized', () => { if (!failed) { initialized = true; report('ready'); } }, { once: true });
    document.body.appendChild(script);
  })();`;
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp(channel)}">${inlineOnlyPolicy}<style>${baseStyle}\n${safeCss(content.css ?? "")}\n${embeddedStyle}</style></head><body>${sanitizeHtml(content.html?.join("") ?? "")}<script nonce="${channel}">${bootstrap}</script></body></html>`;
}

export function validArtifactResizeMessage(data: unknown, channel: string): data is { type: "artifact-resize"; channel: string; height: number } {
  if (!data || typeof data !== "object" || Array.isArray(data)) return false;
  const message = data as Record<string, unknown>;
  return Object.keys(message).length === 3 && message.type === "artifact-resize" && message.channel === channel &&
    typeof message.height === "number" && Number.isSafeInteger(message.height) && message.height > 0;
}

export function validArtifactMessage(data: unknown, channel: string): data is ArtifactStatusMessage {
  if (!data || typeof data !== "object" || Array.isArray(data)) return false;
  const d = data as Record<string, unknown>;
  if (d.channel !== channel || d.type !== "artifact-status" || (d.status !== "ready" && d.status !== "error")) return false;
  if (Object.keys(d).length === 3) return true;
  if (d.status !== "error" || Object.keys(d).length !== 4 || !Object.hasOwn(d, "error") ||
    !d.error || typeof d.error !== "object" || Array.isArray(d.error)) return false;
  const error = d.error as Record<string, unknown>;
  return Object.keys(error).length === 3 && typeof error.name === "string" &&
    error.name.length > 0 && error.name.length <= 40 && typeof error.message === "string" &&
    error.message.length > 0 && error.message.length <= 240 &&
    !/[\u0000-\u001f\u007f]/.test(error.name + error.message) &&
    (error.stage === "initialization" || error.stage === "runtime");
}
