// Switches between Home, Working and Done. The mic on Home and the orb in the top bar share
// a layoutId inside one LayoutGroup, so starting a case morphs one into the other.

import { useState } from "react";
import { AnimatePresence, LayoutGroup, MotionConfig, motion } from "motion/react";
import { Toaster, toast } from "sonner";
import { TopBar } from "./components/TopBar";
import { createCase } from "./lib/api";
import { unlockAudio } from "./lib/audio";
import { useStoredState } from "./lib/hooks";
import { EASE_OUT } from "./lib/motion";
import { isLive, statusLine } from "./lib/state";
import { useCaseRun } from "./lib/useCaseRun";
import { Done } from "./screens/Done";
import { Home } from "./screens/Home";
import { Working } from "./screens/Working";

type View = "home" | "working" | "done" | "error";

export default function App() {
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [voice, setVoice] = useStoredState("lana.voice", true);
  const [clinics, setClinics] = useStoredState<2 | 3>("lana.clinics", 2);
  const { state, start, stop } = useCaseRun();

  async function startWatch() {
    if (busy) return;
    unlockAudio(); // must run inside the click
    setBusy(true);
    try {
      const id = await createCase("watch", voice, clinics);
      start(id);
      setRunning(true);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't start. Try again.");
    } finally {
      setBusy(false);
    }
  }

  function startOver() {
    stop();
    setRunning(false);
  }

  const failed = state.error !== null || state.caseStatus === "error";
  const done = state.offers !== null && state.caseStatus === "offers_ready";
  const view: View = !running ? "home" : failed ? "error" : done ? "done" : "working";

  return (
    <MotionConfig reducedMotion="user">
      <LayoutGroup>
        {/* Screens share one grid cell, so the outgoing one overlaps the incoming one. */}
        <div className="grid min-h-dvh *:[grid-area:1/1]">
          <AnimatePresence initial={false}>
            {view === "home" && (
              <Home
                key="home"
                busy={busy}
                onStart={startWatch}
                voice={voice}
                setVoice={setVoice}
                clinics={clinics}
                setClinics={setClinics}
              />
            )}
            {(view === "working" || view === "done") && (
              <motion.div key="run" className="flex min-h-dvh flex-col" exit={{ opacity: 0, transition: { duration: 0.16 } }}>
                <TopBar
                  status={statusLine(state)}
                  live={isLive(state)}
                  action={view === "done" ? { label: "Start over", onClick: startOver } : { label: "Stop", onClick: startOver }}
                />
                <div className="grid flex-1 *:[grid-area:1/1]">
                  <AnimatePresence initial={false}>
                    {view === "working" ? (
                      <Working key="working" state={state} />
                    ) : (
                      <Done key="done" offers={state.offers ?? []} onStartOver={startOver} />
                    )}
                  </AnimatePresence>
                </div>
              </motion.div>
            )}
            {view === "error" && (
              <motion.main
                key="error"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.2, ease: EASE_OUT }}
                className="grid min-h-dvh place-items-center px-4"
              >
                <div role="alert" className="w-full max-w-[440px] rounded-[20px] border border-hairline bg-surface p-7 shadow-soft">
                  <p className="text-[18px] font-medium text-ink">Lana had to stop</p>
                  <p className="mt-2 text-[15px] leading-[1.55] text-muted">
                    {state.error?.message ?? "Something went wrong during the run. Start over to try again."}
                  </p>
                  <button
                    type="button"
                    onClick={startOver}
                    className="pressable mt-6 rounded-full bg-primary px-5 py-2.5 text-[15px] font-medium text-white hover:bg-primary-press"
                  >
                    Start over
                  </button>
                </div>
              </motion.main>
            )}
          </AnimatePresence>
        </div>
      </LayoutGroup>
      <Toaster
        position="bottom-center"
        toastOptions={{
          style: {
            background: "var(--color-surface)",
            color: "var(--color-ink)",
            border: "1px solid var(--color-hairline)",
            borderRadius: "16px",
            fontFamily: "var(--font-sans)",
            fontSize: "14px",
            boxShadow: "var(--shadow-soft)",
          },
        }}
      />
    </MotionConfig>
  );
}
