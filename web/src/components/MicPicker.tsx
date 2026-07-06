interface Props {
  devices: MediaDeviceInfo[];
  value: string | undefined;
  onChange: (deviceId: string) => void;
  disabled: boolean;
}

// Microphone selector — labels populate after the first permission grant.
export function MicPicker({ devices, value, onChange, disabled }: Props) {
  return (
    <label className="flex flex-col gap-1.5 text-xs text-slate-400">
      <span className="uppercase tracking-wider">Microphone</span>
      <select
        className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 outline-none focus:border-indigo-500 disabled:opacity-50"
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {devices.length === 0 && <option value="">Grant mic access to list devices</option>}
        {devices.map((d, i) => (
          <option key={d.deviceId} value={d.deviceId}>
            {d.label || `Microphone ${i + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}
