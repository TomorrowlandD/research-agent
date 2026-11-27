import { useEffect, useId, useRef, useState } from 'react';
import type { SessionMetrics } from '../types/agent';
import { MOCK_CONTEXT_LIMIT } from '../data/mockData';

const formatTokens = new Intl.NumberFormat('en', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

export function ContextUsage({ usage }: { usage?: SessionMetrics['contextTokens'] }) {
  const [open, setOpen] = useState(false);
  const [hint, setHint] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const tooltipId = useId();
  const panelId = useId();
  useEffect(() => {
    if (!open) return;
    const dismissOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !container.current?.contains(event.target)) setOpen(false);
    };
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        setHint(false);
      }
    };
    document.addEventListener('pointerdown', dismissOutside);
    document.addEventListener('keydown', dismissOnEscape);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside);
      document.removeEventListener('keydown', dismissOnEscape);
    };
  }, [open]);
  const entries = [
    { label: '系统提示词', count: usage?.system ?? 0, color: 'bg-zinc-400' },
    { label: '工具定义', count: usage?.tools ?? 0, color: 'bg-violet-400' },
    { label: '对话消息', count: usage?.messages ?? 0, color: 'bg-blue-500' },
  ];
  const used = entries.reduce((total, entry) => total + entry.count, 0);
  const percent = Math.min(100, used / MOCK_CONTEXT_LIMIT * 100);
  const label = '上下文已用 ' + Math.round(percent) + '%';

  return (
    <div ref={container} className="relative shrink-0" onMouseEnter={() => setHint(true)}
      onMouseLeave={() => setHint(false)} onBlur={() => { setHint(false); setOpen(false); }}
      onKeyDown={event => { if (event.key === 'Escape') setHint(false); }}>
      <button type="button" aria-label={label} aria-expanded={open} aria-controls={open ? panelId : undefined}
        aria-describedby={hint && !open ? tooltipId : undefined}
        onFocus={() => setHint(true)} onClick={() => { setHint(false); setOpen(value => !value); }}
        className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100 focus-visible:outline-blue-500">
        <span aria-hidden="true" className="grid h-4 w-4 place-items-center rounded-full"
          style={{ background: `conic-gradient(currentColor ${percent}%, #e4e4e7 0)` }}>
          <span className="h-[11px] w-[11px] rounded-full bg-white" />
        </span>
      </button>
      {hint && !open && (
        <span role="tooltip" id={tooltipId} className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-md bg-zinc-800 px-2 py-1 text-xs text-white">
          上下文已用
        </span>
      )}
      {open && (
        <div className="absolute bottom-full left-1/2 z-30 w-60 -translate-x-1/2 pb-1.5">
          <div role="region" aria-label="上下文用量明细" id={panelId} className="rounded-xl border border-zinc-200 bg-white p-3 text-xs text-zinc-600 shadow-lg">
            <div className="flex justify-between gap-2">
              <span>{label}</span>
              <span className="font-medium text-zinc-800">{formatTokens.format(used)} / {formatTokens.format(MOCK_CONTEXT_LIMIT)}</span>
            </div>
            <div aria-hidden="true" className="my-3 flex h-1 overflow-hidden rounded-full bg-zinc-100">
              {entries.map(entry => <span key={entry.label} className={entry.color} style={{ width: (entry.count / MOCK_CONTEXT_LIMIT * 100) + '%' }} />)}
            </div>
            <dl className="space-y-1.5">
              {entries.map(entry => (
                <div key={entry.label} className="flex items-center justify-between">
                  <dt className="flex items-center gap-2"><span aria-hidden="true" className={'h-2 w-2 rounded-sm ' + entry.color} />{entry.label}</dt>
                  <dd className="tabular-nums">{formatTokens.format(entry.count)}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
      )}
    </div>
  );
}
