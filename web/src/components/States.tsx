// Shared loading + empty states, so every list page looks consistent on mobile
// and desktop.

export function Loader({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2.5 py-16 text-sm text-neutral-500">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
      {label}
    </div>
  );
}

export function EmptyState({
  icon, title, hint,
}: { icon?: React.ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-neutral-200 px-6 py-14 text-center dark:border-neutral-800">
      <div className="grid h-12 w-12 place-items-center rounded-full bg-neutral-100 text-neutral-400 dark:bg-neutral-800/60 dark:text-neutral-500">
        {icon ?? <DefaultIcon />}
      </div>
      <p className="text-sm font-medium text-neutral-700 dark:text-neutral-200">{title}</p>
      {hint && <p className="max-w-xs text-sm text-neutral-500 dark:text-neutral-400">{hint}</p>}
    </div>
  );
}

function DefaultIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}
