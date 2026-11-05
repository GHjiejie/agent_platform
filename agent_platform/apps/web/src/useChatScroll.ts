import { useCallback, useLayoutEffect, useRef, useState } from "react";

export function useChatScroll(sessionId: string | null) {
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const previousTop = useRef(0);
  const [showLatest, setShowLatest] = useState(false);

  const alignBottom = useCallback(() => {
    const element = viewport.current;
    if (!element || !following.current) return;
    const bottom = Math.max(0, element.scrollHeight - element.clientHeight);
    if (Math.abs(element.scrollTop - bottom) > 1) element.scrollTop = bottom;
    previousTop.current = element.scrollTop;
  }, []);

  const followLatest = useCallback(() => {
    following.current = true;
    setShowLatest(false);
    alignBottom();
  }, [alignBottom]);

  useLayoutEffect(followLatest, [sessionId, followLatest]);

  useLayoutEffect(() => {
    const element = viewport.current;
    const body = content.current;
    if (!element || !body) return;
    const detach = () => {
      if (!following.current) return;
      following.current = false;
      setShowLatest(true);
    };
    const onScroll = () => {
      const top = element.scrollTop;
      const atBottom = element.scrollHeight - top - element.clientHeight <= 4;
      if (atBottom) {
        if (!following.current) {
          following.current = true;
          setShowLatest(false);
        }
      } else if (top < previousTop.current - 1) detach();
      // A growing message can move the bottom without moving the reader.
      // Only an upward scroll detaches; layout growth never opts the user out.
      previousTop.current = top;
    };
    const onWheel = (event: WheelEvent) => { if (event.deltaY < 0 && element.scrollTop > 0) detach(); };
    let touchY: number | undefined;
    const onTouchStart = (event: TouchEvent) => { touchY = event.touches[0]?.clientY; };
    const onTouchMove = (event: TouchEvent) => {
      const next = event.touches[0]?.clientY;
      if (touchY !== undefined && next !== undefined && next > touchY + 2) detach();
      touchY = next;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target !== element || event.defaultPrevented || event.altKey || event.metaKey) return;
      if (event.key === "End") {
        event.preventDefault();
        followLatest();
        return;
      }
      const up = ["ArrowUp", "PageUp", "Home"].includes(event.key) || event.key === " " && event.shiftKey;
      const down = ["ArrowDown", "PageDown", " "].includes(event.key);
      if (!up && !down) return;
      event.preventDefault();
      if (up) detach();
      const distance = event.key.startsWith("Page") || event.key === " " ? element.clientHeight * 0.9 : 40;
      element.scrollTop = event.key === "Home" ? 0 : element.scrollTop + (up ? -distance : distance);
      onScroll();
    };
    // Observe actual layout, including nested component updates, folding,
    // iframe sizing and composer/viewport resizing. Align before the next paint.
    const observer = new ResizeObserver(alignBottom);
    observer.observe(element);
    observer.observe(body);
    element.addEventListener("scroll", onScroll, { passive: true });
    element.addEventListener("wheel", onWheel, { passive: true });
    element.addEventListener("touchstart", onTouchStart, { passive: true });
    element.addEventListener("touchmove", onTouchMove, { passive: true });
    element.addEventListener("keydown", onKeyDown);
    alignBottom();
    return () => {
      observer.disconnect();
      element.removeEventListener("scroll", onScroll);
      element.removeEventListener("wheel", onWheel);
      element.removeEventListener("touchstart", onTouchStart);
      element.removeEventListener("touchmove", onTouchMove);
      element.removeEventListener("keydown", onKeyDown);
    };
  }, [alignBottom, followLatest]);

  return { viewport, content, showLatest, followLatest };
}
