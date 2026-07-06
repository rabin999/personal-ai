interface Props {
  devices: MediaDeviceInfo[];
  value: string | undefined;
  onChange: (deviceId: string) => void;
  disabled: boolean;
}

const FIELD =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800/80 dark:text-slate-100";

// Microphone selector — enumerates every audio input; labels populate after the
// first permission grant (listMicrophones in lib/audio).
export function MicPicker({ devices, value, onChange, disabled }: Props) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5">
      <span className="text-[11px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
        Microphone
      </span>
      <select
        className={FIELD}
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {devices.length === 0 && (
          <option value="">Grant mic access to list devices</option>
        )}
        {devices.map((d, i) => (
          <option key={d.deviceId} value={d.deviceId}>
            {d.label || `Microphone ${i + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}
