import { memo } from "react";
import Markdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remend from "remend";
import { useStreamingText } from "./StreamingText";

const plugins = [remarkGfm];
const components: Components = {
  a: ({ href, children, title }) => href
    ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>
    : <span>{children}</span>,
  img: ({ src, alt }) => src
    ? <a href={src} target="_blank" rel="noopener noreferrer">{alt || "查看图片"}</a>
    : <span>{alt}</span>,
  table: ({ children }) => <div className="pg-markdown-table"><table>{children}</table></div>,
};

export const MessageMarkdown = memo(function MessageMarkdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const visible = useStreamingText(text, streaming);
  const content = streaming ? remend(visible, { linkMode: "text-only", images: false, katex: false }) : visible;
  return <div className={`pg-markdown${streaming ? " is-streaming" : ""}`}>
    <Markdown remarkPlugins={plugins} components={components} skipHtml>{content}</Markdown>
  </div>;
});
