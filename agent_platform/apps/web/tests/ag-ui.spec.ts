import { expect, test } from "@playwright/test";
import { EventType } from "@ag-ui/core";
import type { AgentFrame } from "../src/ag-ui";
import { decodeAgentEvents, messageRunId, reduceMessages } from "../src/ag-ui";

async function decode(text: string, split: number) {
  const bytes = new TextEncoder().encode(text);
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let index = 0; index < bytes.length; index += split) controller.enqueue(bytes.slice(index, index + split));
      controller.close();
    },
  });
  const result: AgentFrame[] = [];
  for await (const frame of decodeAgentEvents(stream)) result.push(frame);
  return result;
}

test("AG-UI SSE 解码支持 UTF-8、CRLF、注释、多行 data 和任意网络分片", async () => {
  const data = ': heartbeat\r\n\r\nid: 3.0\r\ndata: {"type":"TEXT_MESSAGE_START",\r\ndata: "messageId":"answer","role":"assistant"}\r\n\r\nid: 3.1\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"answer","delta":"你好，世界🌏"}\n\nid: 4.0\rdata: {"type":"TEXT_MESSAGE_END","messageId":"answer"}\r\r';
  for (const size of [1, 2, 3, 17, 4096]) {
    const events = await decode(data, size);
    expect(events.map((frame) => frame.cursor)).toEqual(["3.0", "3.1", "4.0"]);
    const messages = events.reduce((previous, frame) => reduceMessages(previous, frame.event), [] as Parameters<typeof reduceMessages>[0]);
    expect(messages).toEqual([{ id: "answer", role: "assistant", content: "你好，世界🌏" }]);
  }
});

test("截断帧不提交游标，无效协议事件拒绝解析", async () => {
  const truncated = await decode('id: 1.0\ndata: {"type":"TEXT_MESSAGE_START","messageId":"a"}\n\nid: 2.0\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"a","delta":"丢失', 2);
  expect(truncated.map((frame) => frame.cursor)).toEqual(["1.0"]);
  await expect(decode('id: 2.0\ndata: {"type":"TEXT_MESSAGE_CONTENT","messageId":"a"}\n\n', 3)).rejects.toThrow("AG-UI");
  await expect(decode('id: 2.0\ndata: {"type":"message.delta","data":{"text":"legacy"}}\n\n', 3)).rejects.toThrow("AG-UI");
  expect(() => reduceMessages([], { type: EventType.TEXT_MESSAGE_CONTENT, messageId: "missing", delta: "lost" })).toThrow("开始事件");
});

test("工具参数与结果累积到对应消息，快照可替换已重放的内容", async () => {
  const events = await decode([
    { type: "TOOL_CALL_START", toolCallId: "tool-1", toolCallName: "calculator", parentMessageId: "assistant" },
    { type: "TOOL_CALL_ARGS", toolCallId: "tool-1", delta: '{"expression":' },
    { type: "TOOL_CALL_ARGS", toolCallId: "tool-1", delta: '"2+3"}' },
    { type: "TOOL_CALL_END", toolCallId: "tool-1" },
    { type: "TOOL_CALL_RESULT", messageId: "result", toolCallId: "tool-1", content: '{"result":5}', role: "tool" },
  ].map((event, index) => `id: ${index + 1}.0\ndata: ${JSON.stringify(event)}\n\n`).join(""), 7);
  const messages = events.reduce((previous, frame) => reduceMessages(previous, frame.event), [] as Parameters<typeof reduceMessages>[0]);
  expect(messages).toMatchObject([
    { id: "assistant", role: "assistant", toolCalls: [{ id: "tool-1", function: { name: "calculator", arguments: '{"expression":"2+3"}' } }] },
    { id: "result", role: "tool", toolCallId: "tool-1", content: '{"result":5}' },
  ]);
  expect(reduceMessages(messages, { type: EventType.MESSAGES_SNAPSHOT, messages: [{ id: "final", role: "assistant", content: "5" }] })).toEqual([{ id: "final", role: "assistant", content: "5" }]);
});

test("生成界面 Activity 按标识替换，结束和恢复快照保留完整代码", async () => {
  const activity = { type: EventType.ACTIVITY_SNAPSHOT, messageId: "artifact:run:1:ui", activityType: "GENERATIVE_UI", content: { html: ["<button>增加</button>"], generating: false }, replace: true };
  const events = await decode(`id: 7.0\ndata: ${JSON.stringify(activity)}\n\n`, 3);
  const once = reduceMessages([], events[0].event);
  expect(reduceMessages(once, events[0].event)).toEqual(once);
  expect(reduceMessages([], { type: EventType.MESSAGES_SNAPSHOT, messages: once })).toEqual(once);
  expect(once[0]).toMatchObject({ role: "activity", activityType: "GENERATIVE_UI", content: activity.content });
});

test("推理增量独立于正文，重连快照和中断状态保留内容", async () => {
  const id = "reasoning:run:call";
  const values = [
    { type: "REASONING_START", messageId: id },
    { type: "REASONING_MESSAGE_START", messageId: id, role: "reasoning" },
    { type: "REASONING_MESSAGE_CONTENT", messageId: id, delta: "检查条件。" },
    { type: "REASONING_MESSAGE_CONTENT", messageId: id, delta: "确认结果。" },
    { type: "REASONING_MESSAGE_END", messageId: id },
    { type: "REASONING_END", messageId: id, metadata: { platform: { reasoning_status: "interrupted" } } },
  ];
  for (const split of [1, 3, 4096]) {
    const frames = await decode(values.map((value, index) => `id: ${index + 1}.0\ndata: ${JSON.stringify(value)}\n\n`).join(""), split);
    const messages = frames.reduce((previous, frame) => reduceMessages(previous, frame.event), [] as Parameters<typeof reduceMessages>[0]);
    expect(messages).toEqual([{ id, role: "reasoning", content: "检查条件。确认结果。", metadata: { platform: { reasoning_status: "interrupted" } } }]);
    expect(reduceMessages(messages, { type: EventType.MESSAGES_SNAPSHOT, messages })).toEqual(messages);
    expect(reduceMessages(messages, { type: EventType.MESSAGES_SNAPSHOT, messages: [{ id: "answer", role: "assistant", content: "结果" }] })).toContainEqual(messages[0]);
  }
  expect(() => reduceMessages([], { type: EventType.REASONING_MESSAGE_CONTENT, messageId: id, delta: "lost" })).toThrow("开始事件");
  expect(() => reduceMessages([{ id, role: "assistant", content: "正文" }], { type: EventType.REASONING_MESSAGE_START, messageId: id, role: "reasoning" })).toThrow("冲突");
  expect(messageRunId({ id: "user-message", role: "user", content: "停止后继续", metadata: { platform: { run_id: "cancelled-run" } } })).toBe("cancelled-run");
  expect(messageRunId({ id: "assistant:complete-run", role: "assistant", content: "正文" })).toBe("complete-run");
  expect(messageRunId({ id: "untrusted", role: "user", content: "消息", metadata: { platform: { run_id: { invalid: true } } } })).toBeUndefined();
});
