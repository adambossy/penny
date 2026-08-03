import { expect, test } from "./fixtures/app";

/**
 * Regression: the page itself must never scroll — the transcript scrolls
 * inside its own container and the composer stays pinned at the bottom of
 * the viewport.
 *
 * The historical bug: an absolutely-positioned `sr-only` marker (since
 * removed) sat inside the transcript, and the transcript scroller was not a
 * positioned ancestor — so the marker's containing block was the AppShell
 * root, its static position sat at the end of the full transcript height,
 * and a long conversation stretched `document` scrolling far past the
 * composer. The scroller stays `relative` to contain any such descendant.
 *
 * Hydration is stubbed with a long transcript so the spec needs no model.
 */
test("long transcript scrolls internally; the document does not scroll", async ({ page }) => {
  const filler = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. ".repeat(8);
  const messages = Array.from({ length: 20 }, (_, i) => ({
    id: `m${i}`,
    role: i % 2 ? "assistant" : "user",
    parts: [{ type: "text", text: `Message ${i}: ${filler}` }],
  }));
  await page.route("**/api/sessions/*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      // `model`: the pinned model the session endpoint now reports; the UI
      // destructures it for the composer chip. Any key works here — the spec
      // asserts layout, not the chip — the legacy constant keeps it faithful.
      body: JSON.stringify({ messages, model: "gemini-3.6-flash" }),
    }),
  );

  await page.goto("/c/11111111-1111-4111-8111-111111111111");
  const transcript = page.getByTestId("transcript");
  await expect(transcript).toContainText("Message 19");

  const layout = await page.evaluate(() => {
    const doc = document.scrollingElement!;
    const scroller = document.querySelector('[data-testid="transcript"]')!.parentElement!;
    return {
      docScrollHeight: doc.scrollHeight,
      docClientHeight: doc.clientHeight,
      scrollerScrollHeight: scroller.scrollHeight,
      scrollerClientHeight: scroller.clientHeight,
    };
  });

  // The document has nothing to scroll; the transcript container does.
  expect(layout.docScrollHeight).toBe(layout.docClientHeight);
  expect(layout.scrollerScrollHeight).toBeGreaterThan(layout.scrollerClientHeight);

  // The composer is pinned inside the viewport.
  const composer = page.getByRole("textbox", { name: "Chat input" });
  const box = await composer.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
});
