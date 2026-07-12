import { useEffect, useState } from "react";

// Renders a Mermaid diagram. Mermaid is imported dynamically so its (large) bundle
// only loads on the page that actually uses it (/how-it-works), not the main app.
export function Mermaid({ chart, dark }: { chart: string; dark: boolean }) {
  const [svg, setSvg] = useState<string>("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: dark ? "dark" : "default",
          themeVariables: { fontFamily: "inherit", fontSize: "14px" },
          flowchart: { curve: "basis", padding: 12, useMaxWidth: true },
        });
        // Fresh id each render — mermaid injects a temp node under this id.
        const id = "mmd-" + Math.random().toString(36).slice(2);
        const { svg } = await mermaid.render(id, chart);
        if (active) setSvg(svg);
      } catch {
        if (active) setFailed(true);
      }
    })();
    return () => {
      active = false;
    };
  }, [chart, dark]);

  if (failed) {
    return (
      <p className="rounded-xl border border-slate-200 bg-white/60 p-4 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-900/50">
        Diagram couldn't render here — the architecture is described in the sections below.
      </p>
    );
  }
  return (
    <div
      className="mermaid-wrap overflow-x-auto [&_svg]:mx-auto [&_svg]:h-auto"
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
