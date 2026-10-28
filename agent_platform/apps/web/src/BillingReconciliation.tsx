import { Select } from "./Select";
import { useRef, useState } from "react";
import {
  CheckCircle2,
  CircleHelp,
  Pencil,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { api, dateTime, money, write } from "./api";
import {
  Badge,
  Button,
  Empty,
  Field,
  Form,
  Modal,
  Panel,
  ResourceState,
  formValue,
  useResource,
  useToast,
} from "./components";
import type { Wallet } from "./types";

type Reservation = {
  id: string;
  call_id: string;
  run_id: string;
  model_id: string;
  user_id: string;
  amount: string;
  status: string;
  reason: string;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_input_tokens: number;
  max_output_tokens: number;
  cost: string;
  created_at: string;
};

export function BillingReconciliation({
  wallet,
  changed,
  basePath,
}: {
  wallet: Wallet | null;
  basePath: string;
  changed: () => Promise<void>;
}) {
  const resource = useResource<{ items: Reservation[] }>(
    `${basePath}/billing/reservations`,
  );
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState<Reservation | null>(null);
  const [action, setAction] = useState("");
  const [unblock, setUnblock] = useState(false);
  const key = useRef("");
  const toast = useToast();
  const unresolved =
    resource.data?.items.filter((item) => item.status === "unresolved") ?? [];
  const items = all ? (resource.data?.items ?? []) : unresolved;
  return (
    <div className="reconciliation">
      {wallet?.blocked && (
        <div className="unblock-row">
          <span>处理待对账记录并核查冻结原因后，可以恢复钱包调用。</span>
          <Button
            variant="secondary"
            onClick={() => setUnblock(true)}
            disabled={
              resource.loading ||
              Boolean(resource.error) ||
              unresolved.length > 0
            }
          >
            <ShieldCheck size={15} />
            解除钱包暂停
          </Button>
        </div>
      )}
      <Panel
        title="预占与对账"
        detail="结果未知的调用保留额度，等待用量证据或人工处理。"
        action={
          <Button
            variant="ghost"
            aria-label="刷新预占"
            onClick={() => void resource.reload()}
          >
            <RefreshCw size={16} />
          </Button>
        }
      >
        <div className="reconciliation-tabs">
          <button
            className={!all ? "active" : ""}
            onClick={() => setAll(false)}
          >
            待人工对账<span>{unresolved.length}</span>
          </button>
          <button className={all ? "active" : ""} onClick={() => setAll(true)}>
            全部预占记录
          </button>
        </div>
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>调用 / 原因</th>
                    <th>状态</th>
                    <th>预占金额</th>
                    <th>已结算费用</th>
                    <th>时间</th>
                    <th className="align-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <strong className="mono" title={item.call_id}>
                          {item.call_id.slice(0, 16)}
                        </strong>
                        <small className="reservation-reason">
                          {item.reason || "—"}
                        </small>
                      </td>
                      <td>
                        <Badge status={item.status} />
                      </td>
                      <td className="numeric">{money(item.amount, 6)}</td>
                      <td className="numeric">
                        {item.status === "unresolved"
                          ? "待确认"
                          : money(item.cost, 6)}
                      </td>
                      <td className="muted nowrap">
                        {dateTime(item.created_at)}
                      </td>
                      <td className="align-right">
                        {item.status === "unresolved" ? (
                          <Button
                            variant="ghost"
                            onClick={() => {
                              key.current = crypto.randomUUID();
                              setAction("");
                              setEditing(item);
                            }}
                          >
                            <Pencil size={14} />
                            人工处理
                          </Button>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title={all ? "暂无预占记录" : "没有待人工对账的调用"}
              description={
                all
                  ? "每次模型调用准入时会创建费用预占。"
                  : "未确认的调用会保留预占，并显示在这里。"
              }
            />
          )}
        </ResourceState>
      </Panel>
      {editing && (
        <Modal
          title="处理待对账调用"
          description={`调用 ${editing.call_id} · 预占 ${money(editing.amount, 6)}`}
          close={() => setEditing(null)}
        >
          <Form
            close={() => setEditing(null)}
            label={
              action === "write_off" ? "确认核销并释放预占" : "确认用量并结算"
            }
            submit={async (form) => {
              if (!action) throw new Error("请选择明确的处理方式。");
              await api(`${basePath}/billing/reservations/${editing.call_id}/resolve`, {
                method: "POST",
                body: JSON.stringify({
                  action,
                  reason: formValue(form, "reason"),
                  ...(action === "confirm"
                    ? {
                        input_tokens: Number(formValue(form, "input_tokens")),
                        output_tokens: Number(formValue(form, "output_tokens")),
                      }
                    : {}),
                }),
                headers: { "Idempotency-Key": key.current },
              });
              setEditing(null);
              toast(
                action === "confirm"
                  ? "用量已确认并结算"
                  : "调用已核销，预占已释放",
              );
              await Promise.all([resource.reload(), changed()]);
            }}
          >
            <div className="inline-note">
              <CircleHelp size={17} />
              {editing.reason ||
                "上游用量尚未得到确认。请根据网关日志、供应商记录或明确核销决定处理。"}
            </div>
            <Field label="处理方式">
              <Select
                value={action}
                onChange={(event) => setAction(event.target.value)}
                required
              >
                <option value="" disabled>
                  选择处理方式
                </option>
                <option value="confirm">根据证据确认实际用量并结算</option>
                <option value="write_off">人工核销此次调用并释放预占</option>
              </Select>
            </Field>
            {action === "confirm" && (
              <>
                <div className="form-columns">
                  <Field label="实际输入 Token">
                    <input
                      name="input_tokens"
                      type="number"
                      min={0}
                      step={1}
                      required
                      placeholder="按证据填写"
                    />
                  </Field>
                  <Field label="实际输出 Token">
                    <input
                      name="output_tokens"
                      type="number"
                      min={0}
                      step={1}
                      required
                      placeholder="按证据填写"
                    />
                  </Field>
                </div>
                <p className="resolution-note">
                  按调用时的价格快照结算。预估用量不能代替确认用量。
                </p>
              </>
            )}
            {action === "write_off" && (
              <div className="form-error">
                <CircleHelp size={17} />
                核销会放弃此次客户收费并释放预占，不表示供应商未产生费用。该决定与原因会永久留在审计记录中。
              </div>
            )}
            <Field
              label="证据 / 处理原因"
              hint="请记录可核查的凭证位置或核销依据，至少 5 个字符。"
            >
              <textarea
                name="reason"
                rows={4}
                minLength={5}
                maxLength={500}
                required
                placeholder="例如：核对 LiteLLM 日志 request_id…，确认输入与输出用量。"
              />
            </Field>
          </Form>
        </Modal>
      )}
      {unblock && (
        <Modal
          title="恢复钱包调用"
          description="恢复后，新调用仍需通过余额、限流与预算校验。"
          close={() => setUnblock(false)}
        >
          <Form
            close={() => setUnblock(false)}
            label="解除暂停"
            submit={async (form) => {
              await write(`${basePath}/billing/unblock`, {
                reason: formValue(form, "reason"),
              });
              setUnblock(false);
              toast("钱包暂停已解除");
              await changed();
            }}
          >
            <div className="inline-note">
              <CheckCircle2 size={17} />
              请确认冻结原因已处理；存在待对账调用时无法解除暂停。
            </div>
            <Field label="处理结果与恢复原因">
              <textarea
                name="reason"
                rows={4}
                minLength={5}
                maxLength={500}
                required
              />
            </Field>
          </Form>
        </Modal>
      )}
    </div>
  );
}
