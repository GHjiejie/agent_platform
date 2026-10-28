import {
  cloneElement,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useId,
  useState,
} from "react";
import type { ButtonHTMLAttributes, FormEvent, ReactNode } from "react";
import {
  AlertCircle,
  ArrowRight,
  Check,
  ChevronRight,
  LoaderCircle,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { api, getTenantId } from "./api";

export { Select } from "./Select";

export function useResource<T>(path: string, enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const revision = useRef(0);
  const scope = useRef(getTenantId()).current;
  const reload = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const current = ++revision.current;
    setError("");
    setLoading(true);
    try {
      const value = await api<T>(path, {}, scope);
      if (current === revision.current) setData(value);
    } catch (err) {
      if (current === revision.current)
        setError(err instanceof Error ? err.message : "请求失败");
    } finally {
      if (current === revision.current) setLoading(false);
    }
  }, [path, enabled, scope]);
  useEffect(() => {
    setData(null);
    void reload();
    return () => {
      revision.current++;
    };
  }, [reload]);
  return { data, error, loading, reload, setData };
}

type Notice = { text: string; kind: "success" | "error" };
const ToastContext = createContext<
  (text: string, kind?: Notice["kind"]) => void
>(() => {});
export const useToast = () => useContext(ToastContext);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  const show = useCallback(
    (text: string, kind: Notice["kind"] = "success") =>
      setNotice({ text, kind }),
    [],
  );
  return (
    <ToastContext.Provider value={show}>
      {children}
      {notice && (
        <div
          role={notice.kind === "error" ? "alert" : "status"}
          className={`toast ${notice.kind}`}
        >
          {notice.kind === "success" ? (
            <Check size={18} />
          ) : (
            <AlertCircle size={18} />
          )}
          <span>{notice.text}</span>
          <button aria-label="关闭通知" onClick={() => setNotice(null)}>
            <X size={16} />
          </button>
        </div>
      )}
    </ToastContext.Provider>
  );
}
export function Button({
  children,
  variant = "primary",
  busy = false,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  busy?: boolean;
}) {
  return (
    <button
      className={`button ${variant} ${className}`}
      {...props}
      disabled={busy || props.disabled}
      aria-busy={busy || undefined}
    >
      {busy ? <LoaderCircle size={16} className="spin" /> : null}
      {children}
    </button>
  );
}
export function PageTitle({
  eyebrow = "WORKSPACE",
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-title">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="page-action">{action}</div>}
    </header>
  );
}
export function Panel({
  title,
  detail,
  action,
  children,
  className = "",
}: {
  title?: string;
  detail?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title && (
        <div className="panel-head">
          <div>
            <h2>{title}</h2>
            {detail && <p>{detail}</p>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}
export function Empty({
  title = "这里还没有记录",
  description = "开始使用后，相关记录会显示在这里。",
  action,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-symbol">
        <Search size={26} strokeWidth={1.4} />
      </div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={22} className="spin" />
      正在加载工作空间…
    </div>
  );
}
export function ErrorState({
  message,
  retry,
}: {
  message: string;
  retry?: () => void;
}) {
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={20} />
      <div>
        <strong>暂时无法加载</strong>
        <p>{message}</p>
      </div>
      {retry && (
        <Button variant="secondary" onClick={retry}>
          <RefreshCw size={15} />
          重试
        </Button>
      )}
    </div>
  );
}
export function ResourceState({
  loading,
  error,
  retry,
  children,
}: {
  loading: boolean;
  error: string;
  retry?: () => void;
  children: ReactNode;
}) {
  return error ? (
    <ErrorState message={error} retry={retry} />
  ) : loading ? (
    <Loading />
  ) : (
    <>{children}</>
  );
}
const statuses: Record<string, [string, string]> = {
  completed: ["已完成", "green"],
  succeeded: ["已完成", "green"],
  expired: ["已超时", "red"],
  cancelling: ["取消中", "amber"],
  written_off: ["已核销", "neutral"],
  confirmed: ["用量已结算", "green"],
  in_flight: ["调用中", "purple"],
  success: ["成功", "green"],
  settled: ["已结算", "green"],
  active: ["已启用", "green"],
  running: ["运行中", "purple"],
  queued: ["排队中", "amber"],
  pending: ["等待中", "amber"],
  reserved: ["预占中", "amber"],
  unresolved: ["待对账", "amber"],
  failed: ["失败", "red"],
  error: ["错误", "red"],
  cancelled: ["已取消", "neutral"],
  canceled: ["已取消", "neutral"],
  disabled: ["已停用", "neutral"],
  released: ["已释放", "neutral"],
  draft: ["草稿", "neutral"],
  interrupted: ["已中断", "red"],
};
export const statusLabel = (status: string) => statuses[status]?.[0] ?? status;

export function Badge({
  status,
  children,
}: {
  status: string;
  children?: ReactNode;
}) {
  const [label, color] = statuses[status] ?? [status, "neutral"];
  return (
    <span className={`badge ${color}`}>
      <i />
      {children ?? label}
    </span>
  );
}
export function SearchBox({
  value,
  onChange,
  placeholder = "搜索…",
  "aria-label": ariaLabel,
  className = "",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  "aria-label"?: string;
  className?: string;
}) {
  return (
    <label className={`search-box ${className}`}>
      <Search size={16} />
      <input
        aria-label={ariaLabel ?? placeholder}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  const generatedId = useId();
  const isControl = isValidElement<{
    id?: string;
    "aria-describedby"?: string;
  }>(children);
  const controlId = isControl
    ? (children.props.id ?? generatedId)
    : generatedId;
  return (
    <div className="field">
      <label htmlFor={controlId}>{label}</label>
      {isControl
        ? cloneElement(children, {
            id: controlId,
            "aria-describedby": hint
              ? `${controlId}-hint`
              : children.props["aria-describedby"],
          })
        : children}
      {hint && <small id={`${controlId}-hint`}>{hint}</small>}
    </div>
  );
}
export function Modal({
  title,
  description,
  close,
  children,
}: {
  title: string;
  description?: string;
  close: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current
      ?.querySelector<HTMLElement>('input:not([aria-hidden="true"]), select:not([aria-hidden="true"]), textarea, button')
      ?.focus();
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
      if (event.key !== "Tab") return;
      const elements = ref.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]):not([aria-hidden="true"]):not([type="hidden"]), select:not([disabled]):not([aria-hidden="true"]), textarea:not([disabled]), [tabindex="0"]',
      );
      if (!elements?.length) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", listener);
    return () => {
      window.removeEventListener("keydown", listener);
      document.body.style.overflow = originalOverflow;
      previous?.focus();
    };
  }, []);
  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div
        ref={ref}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modal-head">
          <div>
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={close}
            aria-label="关闭"
          >
            <X size={20} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
export function Form({
  submit,
  children,
  label = "保存",
  close,
}: {
  submit: (form: HTMLFormElement) => Promise<void>;
  children: ReactNode;
  label?: string;
  close: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(true);
    setError("");
    try {
      await submit(form);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={onSubmit}>
      <div className="form-body">
        {children}
        {error && (
          <div className="form-error" role="alert">
            <AlertCircle size={16} />
            {error}
          </div>
        )}
      </div>
      <div className="modal-footer">
        <Button
          type="button"
          variant="secondary"
          onClick={close}
          disabled={busy}
        >
          取消
        </Button>
        <Button type="submit" busy={busy}>
          {label}
          <ArrowRight size={16} />
        </Button>
      </div>
    </form>
  );
}
export const formValue = (form: HTMLFormElement, key: string) =>
  String(new FormData(form).get(key) ?? "");
export function TextLink({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="text-link" onClick={onClick}>
      {children}
      <ChevronRight size={15} />
    </button>
  );
}
