import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const revealWindow = 100;
const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function useStreamingText(text: string, streaming: boolean) {
  const [displayed, setDisplayed] = useState(text);
  const visible = useRef(text);
  const target = useRef(text);
  const frame = useRef<number | null>(null);
  const pending = useRef<number[]>([]);
  const index = useRef(0);
  const rate = useRef(0);
  const credit = useRef(0);
  const lastTime = useRef(0);
  const reduced = useRef(window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  const publish = useCallback((value: string) => {
    if (visible.current === value) return;
    visible.current = value;
    setDisplayed(value);
  }, []);
  const flush = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    publish(target.current);
  }, [publish]);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const changeMotion = () => {
      reduced.current = preference.matches;
      if (preference.matches) flush();
    };
    const visibility = () => { if (document.hidden) flush(); };
    changeMotion();
    preference.addEventListener("change", changeMotion);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      frame.current = null;
      preference.removeEventListener("change", changeMotion);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [flush]);

  useLayoutEffect(() => {
    target.current = text;
    if (!streaming || reduced.current || document.hidden || !text.startsWith(visible.current)) {
      flush();
      return;
    }
    const offset = visible.current.length;
    pending.current = Array.from(segmenter.segment(text.slice(offset)), (part) => offset + part.index + part.segment.length);
    index.current = 0;
    credit.current = 0;
    rate.current = Math.max(60, pending.current.length * 1000 / revealWindow);
    if (!pending.current.length || frame.current !== null) return;
    lastTime.current = performance.now();
    const reveal = (now: number) => {
      credit.current += rate.current * Math.min(now - lastTime.current, 64) / 1000;
      lastTime.current = now;
      const count = Math.floor(credit.current);
      credit.current -= count;
      index.current = Math.min(pending.current.length, index.current + count);
      if (index.current) publish(target.current.slice(0, pending.current[index.current - 1]));
      frame.current = index.current < pending.current.length ? requestAnimationFrame(reveal) : null;
    };
    frame.current = requestAnimationFrame(reveal);
  }, [text, streaming, flush, publish]);

  // Completion/cancellation and historical snapshots never wait for animation.
  return streaming ? displayed : text;
}

export function StreamingText({ text, streaming }: { text: string; streaming: boolean }) {
  return <>{useStreamingText(text, streaming)}</>;
}
