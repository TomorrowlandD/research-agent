import { useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useAppStore } from '../store/useAppStore';

interface Props {
  value: string;
  onChange(value: string): void;
  onSubmit(): void;
}

export function MessageInput({ value, onChange, onSubmit }: Props) {
  const { skills, sessions, activeWorkspaceId } = useAppStore();
  const textarea = useRef<HTMLTextAreaElement>(null);
  const listId = useId();
  const [trigger, setTrigger] = useState<{ start:number; end:number; token:string } | null>(null);
  const [index, setIndex] = useState(0);
  const workspaceSessions = sessions.filter(s => s.workspaceId === activeWorkspaceId);
  const candidates = trigger?.token[0] === '/'
    ? skills.filter(skill => skill.enabled).map(skill => '/' + skill.name)
    : trigger ? Array.from(new Set(workspaceSessions.flatMap(s => [
        ...s.messages.flatMap(m => (m.toolCalls ?? []).flatMap(tool => tool.diff ? ['@' + tool.diff.filePath] : [])),
        '@' + s.title,
      ]))) : [];
  const options = candidates.filter(name => name.toLowerCase().startsWith(trigger?.token.toLowerCase() ?? ''));
  const selected = Math.min(index, options.length - 1);

  function updateTrigger(text:string, caret:number) {
    const match = text.slice(0, caret).match(/(?:^|\s)([/@][^\s/@]*)$/);
    setTrigger(match ? { start:caret - match[1].length, end:caret, token:match[1] } : null);
    setIndex(0);
  }
  function choose(option:string) {
    if (!trigger) return;
    const prefix = value.slice(0, trigger.start) + option + ' ';
    onChange(prefix + value.slice(trigger.end));
    setTrigger(null);
    requestAnimationFrame(() => textarea.current?.setSelectionRange(prefix.length, prefix.length));
  }
  function handleKey(event:KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (trigger && event.key === 'Escape') {
      event.preventDefault();
      setTrigger(null);
      return;
    }
    if (trigger && options.length && ['ArrowDown','ArrowUp'].includes(event.key)) {
      event.preventDefault();
      setIndex((selected + (event.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length);
      return;
    }
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    if (trigger) {
      if (options[selected]) choose(options[selected]);
      else setTrigger(null);
    } else onSubmit();
  }
  return <div className="relative">
    {trigger && <div id={listId} role="listbox" aria-label={trigger.token[0] === '/' ? '指令' : '引用'}
      className="absolute bottom-full left-0 z-30 mb-2 max-h-44 w-72 max-w-full overflow-auto rounded-lg border border-zinc-200 bg-white p-1 shadow-lg">
      {options.length === 0 && <div className="p-2 text-xs text-zinc-500">暂无匹配项</div>}
      {options.map((option, i) => <button key={option} id={listId + i} type="button" role="option" aria-selected={i === selected}
        tabIndex={-1} onMouseDown={event => event.preventDefault()} onClick={() => choose(option)}
        className={'block w-full truncate rounded px-2 py-1.5 text-left text-xs ' + (i === selected ? 'bg-zinc-100' : 'hover:bg-zinc-50')}>{option}</button>)}
    </div>}
    <textarea ref={textarea} rows={2} value={value} aria-label="消息" aria-autocomplete="list"
      aria-controls={trigger ? listId : undefined} aria-activedescendant={trigger && selected >= 0 ? listId + selected : undefined}
      onChange={event => { onChange(event.target.value); updateTrigger(event.target.value,event.target.selectionStart); }}
      onClick={event => updateTrigger(value,event.currentTarget.selectionStart)}
      onKeyUp={event => {
        if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) updateTrigger(value,event.currentTarget.selectionStart);
      }}
      onBlur={() => setTrigger(null)} onKeyDown={handleKey}
      placeholder="发消息或创建任务，/ 调用指令，@ 文件或对话"
      className="block w-full resize-none bg-transparent text-[14px] text-zinc-800 placeholder:text-zinc-400 focus:outline-none leading-relaxed"/>
  </div>;
}
