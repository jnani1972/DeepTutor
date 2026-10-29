"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { Loader2 } from "lucide-react";

import {
  type ImportJob,
  type ImportSource,
  type PreviewResult,
  createImportJob,
  listImportJobs,
  previewImport,
} from "./api";
import { deepImportRoute, formatDuration } from "./captureFlow";

type SourceMode = "url" | "folder";

const YOUTUBE_PLACEHOLDER = "https://www.youtube.com/playlist?list=…";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function DeepImportStart() {
  const { t } = useTranslation();
  const router = useRouter();
  const [mode, setMode] = useState<SourceMode>("url");
  const [value, setValue] = useState("");
  const [kbName, setKbName] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewSource, setPreviewSource] = useState<ImportSource | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<"" | "preview" | "create">("");
  const [error, setError] = useState("");
  const [jobs, setJobs] = useState<ImportJob[]>([]);

  useEffect(() => {
    listImportJobs()
      .then(setJobs)
      .catch(() => setJobs([]));
  }, []);

  const totalSelected = useMemo(
    () =>
      (preview?.entries ?? [])
        .filter((entry) => selected.has(entry.video_id))
        .reduce((sum, entry) => sum + entry.duration_seconds, 0),
    [preview, selected],
  );

  const runPreview = async () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    const source: ImportSource = mode === "url" ? { url: trimmed } : { folder: trimmed };
    setBusy("preview");
    setError("");
    setPreview(null);
    try {
      const result = await previewImport(source);
      setPreview(result);
      setPreviewSource(source);
      setSelected(new Set(result.entries.map((entry) => entry.video_id)));
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setBusy("");
    }
  };

  const createJob = async () => {
    if (!preview || !previewSource || selected.size === 0) return;
    setBusy("create");
    setError("");
    try {
      const job = await createImportJob({
        ...previewSource,
        video_ids: preview.entries
          .filter((entry) => selected.has(entry.video_id))
          .map((entry) => entry.video_id),
        kb_name: kbName.trim() || undefined,
      });
      router.push(deepImportRoute(job.job_id));
    } catch (caught) {
      setError(messageOf(caught));
      setBusy("");
    }
  };

  const toggle = (videoId: string) =>
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(videoId)) next.delete(videoId);
      else next.add(videoId);
      return next;
    });

  const inputClass =
    "w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-sm text-[var(--foreground)]";
  const primaryClass =
    "inline-flex items-center gap-2 rounded-md bg-[var(--foreground)] px-4 py-2 text-sm font-medium text-[var(--background)] disabled:opacity-50";

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 overflow-y-auto p-6">
      <header>
        <h1 className="text-lg font-semibold text-[var(--foreground)]">{t("Deep import")}</h1>
        <p className="text-sm text-[var(--muted-foreground)]">
          {t(
            "Turn a YouTube playlist or video, or a workspace folder of videos, into a knowledge base. YouTube videos play in this tab while you share it.",
          )}
        </p>
      </header>

      <section className="flex flex-col gap-3 rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
        <div className="flex gap-4 text-sm" role="radiogroup">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              checked={mode === "url"}
              onChange={() => setMode("url")}
            />
            {t("YouTube URL")}
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              checked={mode === "folder"}
              onChange={() => setMode("folder")}
            />
            {t("Workspace folder")}
          </label>
        </div>
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void runPreview();
          }}
        >
          <input
            className={inputClass}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={
              mode === "url"
                ? YOUTUBE_PLACEHOLDER
                : t("Folder path inside the workspace")
            }
            aria-label={mode === "url" ? t("YouTube URL") : t("Workspace folder")}
          />
          <button type="submit" className={primaryClass} disabled={!value.trim() || busy !== ""}>
            {busy === "preview" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {t("Preview")}
          </button>
        </form>
        {error ? <p className="text-sm text-[var(--destructive)]">{error}</p> : null}
      </section>

      {preview ? (
        <section className="flex flex-col gap-3 rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
          <h2 className="font-medium text-[var(--foreground)]">{preview.title}</h2>
          {preview.entries.length === 0 ? (
            <p className="text-sm text-[var(--muted-foreground)]">{t("No videos found.")}</p>
          ) : (
            <ul className="max-h-96 divide-y divide-[var(--border)] overflow-y-auto text-sm">
              {preview.entries.map((entry) => (
                <li key={entry.video_id}>
                  <label className="flex items-center gap-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(entry.video_id)}
                      onChange={() => toggle(entry.video_id)}
                    />
                    <span className="w-6 text-right text-[var(--muted-foreground)]">
                      {entry.position}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{entry.title}</span>
                    <span className="text-[var(--muted-foreground)]">
                      {formatDuration(entry.duration_seconds)}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <p className="text-sm text-[var(--muted-foreground)]">
            {t("{{count}} selected · total {{duration}}", {
              count: selected.size,
              duration: formatDuration(totalSelected),
            })}
          </p>
          {preview.kind !== "folder" ? (
            <p className="text-sm text-[var(--muted-foreground)]">
              {t("Capture runs in real time: the videos play once in this tab.")}
            </p>
          ) : null}
          <label className="flex flex-col gap-1 text-sm">
            {t("Knowledge base name")}
            <input
              className={inputClass}
              value={kbName}
              placeholder={t("Leave blank for an automatic name")}
              onChange={(event) => setKbName(event.target.value)}
            />
          </label>
          <button
            type="button"
            className={`${primaryClass} w-fit`}
            disabled={selected.size === 0 || busy !== ""}
            onClick={() => void createJob()}
          >
            {busy === "create" ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {t("Start import")}
          </button>
        </section>
      ) : null}

      {jobs.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-medium text-[var(--foreground)]">{t("Recent imports")}</h2>
          <ul className="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)] text-sm">
            {jobs.map((job) => (
              <li key={job.job_id}>
                <button
                  type="button"
                  className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-[var(--muted)]"
                  onClick={() => router.push(deepImportRoute(job.job_id))}
                >
                  <span className="min-w-0 flex-1 truncate">{job.title}</span>
                  <span className="text-[var(--muted-foreground)]">
                    {t(`deep_import.status.${job.status}`)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
