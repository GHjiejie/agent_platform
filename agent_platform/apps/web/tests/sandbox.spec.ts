import { expect, test } from "@playwright/test";

test("界面净化移除宿主能力与外部资源，保留 SVG 图表", async ({ page }) => {
  await page.goto("/");
  const sanitized = await page.evaluate(async (modulePath) => {
    const { sanitizeHtml, finalDocument, validArtifactMessage } = await import(modulePath);
    const html = sanitizeHtml('<script>alert(1)</script><iframe src="https://example.test"></iframe><form><input></form><img src="https://example.test/a" onerror="alert(1)"><a href="javascript:alert(1)">link</a><svg viewBox="0 0 10 10"><rect width="10" height="10"/><foreignObject>bad</foreignObject><use href="#x"/></svg>');
    const final = finalDocument({ html: [html], css: '@import "https://example.test/a";p{background:url(https://example.test/a)}', jsExpressions: ['const example="</script><img src=x>";'] }, "test-channel");
    return { html, final, accepted: validArtifactMessage({ channel: "wrong", type: "artifact-status", status: "ready" }, "test-channel") };
  }, "/src/sandbox.ts");
  expect(sanitized.html).not.toMatch(/<script|<iframe|<form|<foreignObject|<use|onerror=|href=|src=/i);
  expect(sanitized.html).toContain("<rect");
  expect(sanitized.final).toContain("connect-src 'none'");
  expect(sanitized.final).not.toContain("https://example.test/a");
  expect(sanitized.final).toContain("\\u003c/script>");
  expect(sanitized.accepted).toBe(false);
});
