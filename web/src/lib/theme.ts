import { useEffect, useState } from "react";

export type Mode = "light" | "dark";

// Reference categorical palette: same eight hues, stepped separately for each surface.
export const SERIES: Record<Mode, string[]> = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};
export const TOKENS: Record<Mode, { surface: string; ink: string; ink2: string; muted: string; hairline: string; baseline: string; ring: string }> = {
  light: { surface: "#fcfcfb", ink: "#0b0b0b", ink2: "#52514e", muted: "#898781", hairline: "#e1e0d9", baseline: "#c3c2b7", ring: "rgba(11,11,11,0.10)" },
  dark: { surface: "#1a1a19", ink: "#ffffff", ink2: "#c3c2b7", muted: "#898781", hairline: "#2c2c2a", baseline: "#383835", ring: "rgba(255,255,255,0.10)" },
};

function currentMode(): Mode {
  const forced = document.documentElement.dataset.theme;
  if (forced === "light" || forced === "dark") return forced;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useMode(): Mode {
  const [mode, setMode] = useState<Mode>(currentMode);
  useEffect(() => {
    const update = () => setMode(currentMode());
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", update);
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      mq.removeEventListener("change", update);
      obs.disconnect();
    };
  }, []);
  return mode;
}

export type Pref = "system" | "light" | "dark";

export function applyPref(pref: Pref) {
  if (pref === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = pref;
  try {
    localStorage.setItem("fin-theme", pref);
  } catch {
    /* storage can be unavailable; the choice just won't persist */
  }
}

export function loadPref(): Pref {
  try {
    const v = localStorage.getItem("fin-theme");
    if (v === "light" || v === "dark") return v;
  } catch {
    /* ignore */
  }
  return "system";
}

/** Entities keep their color regardless of what else is on screen. */
export const ENTITY_SLOT: Record<string, number> = {
  recurring: 0, regular: 1, one_off: 2,
  essential: 0, discretionary: 1,
  Cash: 0, Investments: 1, Pension: 2, Property: 3, Vehicles: 4,
};
