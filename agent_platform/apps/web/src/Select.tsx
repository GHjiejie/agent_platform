import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent, ReactNode, SelectHTMLAttributes } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Search } from "lucide-react";

type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, "multiple" | "size" | "value" | "defaultValue" | "onChange"> & {
  value?: string | number;
  defaultValue?: string | number;
  onChange?: (event: ChangeEvent<HTMLSelectElement>) => void;
  icon?: ReactNode;
  variant?: "default" | "quiet";
  searchable?: boolean;
  emptyLabel?: string;
};
type Option = { value: string; label: string; disabled: boolean; description: string };
type Position = { left: number; top?: number; bottom?: number; width: number; maxHeight: number };

/** A shared accessible picker; the native control owns form values/validation. */
export function Select({
  children, value, defaultValue, onChange, className = "", id: providedId,
  disabled, required, icon, variant = "default", searchable, emptyLabel = "没有匹配的选项",
  "aria-label": ariaLabel, "aria-describedby": describedBy, title, ...props
}: SelectProps) {
  const generatedId = useId();
  const id = providedId ?? generatedId;
  const native = useRef<HTMLSelectElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const [options, setOptions] = useState<Option[]>([]);
  const [internalValue, setInternalValue] = useState(String(defaultValue ?? ""));
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const [invalid, setInvalid] = useState(false);
  const [position, setPosition] = useState<Position | null>(null);
  const selectedValue = value === undefined ? internalValue : String(value);
  const selected = options.find((option) => option.value === selectedValue);
  const filtered = options.filter((option) => `${option.label} ${option.description}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const canSearch = searchable ?? options.length > 6;
  const listId = `${id}-options`;
  const descriptionIds = [describedBy, invalid ? `${id}-error` : undefined].filter(Boolean).join(" ") || undefined;

  useLayoutEffect(() => {
    if (!native.current) return;
    const next = Array.from(native.current.options, (option) => ({
      value: option.value, label: option.textContent?.trim() ?? "",
      disabled: option.disabled || Boolean(option.closest("optgroup")?.disabled),
      description: option.dataset.description ?? "",
    }));
    setOptions((previous) => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    if (value === undefined) setInternalValue(native.current.value);
  }, [children, value]);

  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  useLayoutEffect(() => {
    if (!open) return;
    const updatePosition = () => {
      if (!trigger.current) return;
      const rect = trigger.current.getBoundingClientRect();
      const viewport = window.visualViewport;
      const viewportBottom = (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight);
      const below = viewportBottom - rect.bottom - 16;
      const above = rect.top - (viewport?.offsetTop ?? 0) - 16;
      const desiredHeight = Math.min(360, options.length * 44 + (canSearch ? 55 : 10));
      const upwards = below < desiredHeight && above > below;
      const width = Math.min(Math.max(rect.width, variant === "quiet" ? 260 : 220), window.innerWidth - 24);
      setPosition({
        left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        width, maxHeight: Math.max(80, Math.min(360, upwards ? above : below)),
        ...(upwards ? { bottom: window.innerHeight - rect.top + 6 } : { top: rect.bottom + 6 }),
      });
    };
    updatePosition();
    const outside = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && !trigger.current?.contains(target) && !menu.current?.contains(target)) setOpen(false);
    };
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    window.visualViewport?.addEventListener("resize", updatePosition);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("focusin", outside);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      window.visualViewport?.removeEventListener("resize", updatePosition);
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("focusin", outside);
    };
  }, [open, canSearch, variant, options.length]);

  useLayoutEffect(() => {
    if (open && position && canSearch) search.current?.focus();
  }, [open, Boolean(position), canSearch]);

  useEffect(() => {
    if (open) document.getElementById(`${listId}-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [open, active, listId]);

  function show(direction = 1) {
    if (disabled) return;
    setQuery("");
    const current = options.findIndex((option) => option.value === selectedValue && !option.disabled);
    const candidates = options.map((option, index) => option.disabled ? -1 : index).filter((index) => index >= 0);
    setActive(current >= 0 ? current : direction > 0 ? candidates[0] ?? -1 : candidates.at(-1) ?? -1);
    setOpen(true);
  }
  function close(restoreFocus = false) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  }
  function choose(option: Option) {
    if (option.disabled || !native.current) return;
    if (native.current.value !== option.value) {
      native.current.value = option.value;
      native.current.dispatchEvent(new Event("change", { bubbles: true }));
    }
    close(true);
  }
  function keyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Escape" && open) {
      event.preventDefault(); event.stopPropagation(); close(true);
    } else if (event.key === "Tab" && open) {
      close(true);
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (!open) { show(event.key === "ArrowUp" ? -1 : 1); return; }
      const indices = filtered.map((option, index) => option.disabled ? -1 : index).filter((index) => index >= 0);
      const cursor = indices.indexOf(active);
      setActive(event.key === "Home" ? indices[0] ?? -1 : event.key === "End" ? indices.at(-1) ?? -1 : indices[(cursor + (event.key === "ArrowDown" ? 1 : -1) + indices.length) % indices.length] ?? -1);
    } else if (event.key === "Enter" || event.key === " " && event.currentTarget === trigger.current) {
      event.preventDefault();
      if (!open) show();
      else if (filtered[active]) choose(filtered[active]);
    }
  }

  return <div className={`select-control ${variant === "quiet" ? "select-quiet" : ""} ${className}`}>
    <select
      {...props} ref={native} id={`${id}-native`} className="select-native" aria-hidden="true" tabIndex={-1}
      value={value} defaultValue={defaultValue} disabled={disabled} required={required}
      onChange={(event) => { if (value === undefined) setInternalValue(event.target.value); setInvalid(false); onChange?.(event); }}
      onInvalid={(event) => { event.preventDefault(); setInvalid(true); trigger.current?.focus(); }}
    >{children}</select>
    <button
      ref={trigger} id={id} type="button" className="select-trigger" role="combobox"
      aria-label={ariaLabel} aria-describedby={descriptionIds} aria-expanded={open}
      aria-controls={listId} aria-haspopup="listbox" aria-required={required || undefined}
      aria-invalid={invalid || undefined} aria-activedescendant={open && !canSearch && active >= 0 ? `${listId}-${active}` : undefined}
      disabled={disabled} title={title} onClick={() => open ? close() : show()} onKeyDown={keyDown}
    >
      {icon && <span className="select-icon" aria-hidden="true">{icon}</span>}
      <span className="select-value">{selected?.label || "请选择"}</span>
      <ChevronDown size={15} className={open ? "select-chevron rotated" : "select-chevron"} aria-hidden="true" />
    </button>
    {invalid && <small className="select-validation" id={`${id}-error`}>请选择一项</small>}
    {open && createPortal(
      <div ref={menu} className="select-menu" style={position ?? { visibility: "hidden" }} data-select-menu data-side={position?.bottom === undefined ? "bottom" : "top"}>
        {canSearch && <div className="select-search">
          <Search size={15} aria-hidden="true" />
          <input ref={search} type="search" role="combobox" aria-label="搜索选项" aria-autocomplete="list"
            aria-expanded="true" aria-controls={listId} aria-activedescendant={active >= 0 ? `${listId}-${active}` : undefined}
            placeholder="搜索…" value={query} onKeyDown={keyDown}
            onChange={(event) => {
              const next = event.target.value;
              setQuery(next);
              setActive(options.filter((option) => `${option.label} ${option.description}`.toLocaleLowerCase().includes(next.trim().toLocaleLowerCase())).findIndex((option) => !option.disabled));
            }} />
        </div>}
        <div id={listId} role="listbox" aria-label={ariaLabel ?? "可选项"} className="select-options">
          {filtered.length ? filtered.map((option, index) => <div
            id={`${listId}-${index}`} key={`${option.value}-${index}`} role="option"
            aria-selected={option.value === selectedValue} aria-disabled={option.disabled || undefined}
            className={`select-option ${index === active ? "is-active" : ""}`}
            onPointerMove={() => { if (!option.disabled) setActive(index); }}
            onMouseDown={(event) => event.preventDefault()} onClick={() => choose(option)}
          >
            <span><span className="select-option-label">{option.label}</span>{option.description && <small>{option.description}</small>}</span>
            {option.value === selectedValue && <Check size={16} aria-hidden="true" />}
          </div>) : <div className="select-empty">{emptyLabel}</div>}
        </div>
      </div>, document.body,
    )}
  </div>;
}
