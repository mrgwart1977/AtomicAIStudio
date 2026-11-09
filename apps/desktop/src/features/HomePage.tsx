import { useState } from "react";
import { useApp } from "../stores/appStore";
import { NewProjectDialog } from "./NewProjectDialog";

const CARDS = [
  {
    id: "movie",
    title: "Create a Movie",
    desc: "Start with an idea or screenplay. Review the plan before generation.",
    template: "short_film",
  },
  {
    id: "scene",
    title: "Create a Scene",
    desc: "Make a standalone cinematic sequence with prompts or reusable cast.",
    template: "blank",
  },
  {
    id: "image",
    title: "Create an Image",
    desc: "Create artwork, characters, environments and keyframes.",
    template: "blank",
  },
] as const;

export function HomePage() {
  const [dialog, setDialog] = useState(false);
  const [template, setTemplate] = useState<string>("blank");
  const projects = useApp((s) => s.projects);
  const setProject = useApp((s) => s.setCurrentProject);
  const setPage = useApp((s) => s.setPage);

  const latest = projects.filter((p) => p.status === "active")[0];

  return (
    <div className="mx-auto max-w-4xl p-8">
      <h1 className="text-2xl font-bold">Create something extraordinary</h1>
      <p className="mt-1 text-muted">A simple starting point. No technical setup required to explore.</p>

      <div className="mt-8 grid grid-cols-2 gap-5">
        {CARDS.map((c) => (
          <button
            key={c.id}
            onClick={() => {
              setTemplate(c.template);
              setDialog(true);
            }}
            className="rounded-lg border border-line bg-card p-5 text-left hover:border-teal/50 hover:bg-card-hi"
          >
            <h2 className="text-lg font-semibold">{c.title}</h2>
            <p className="mt-1.5 text-sm text-muted">{c.desc}</p>
          </button>
        ))}
        <button
          onClick={() => {
            if (latest) {
              setProject(latest.id);
              setPage("projects");
            } else {
              setDialog(true);
            }
          }}
          className="rounded-lg border border-line bg-card p-5 text-left hover:border-teal/50 hover:bg-card-hi"
        >
          <h2 className="text-lg font-semibold">Continue a Project</h2>
          <p className="mt-1.5 text-sm text-muted">
            {latest ? `Resume “${latest.name}”.` : "Resume your film. See saved work and the next recommended action."}
          </p>
        </button>
      </div>

      <NewProjectDialog open={dialog} onOpenChange={setDialog} defaultTemplate={template} />
    </div>
  );
}
