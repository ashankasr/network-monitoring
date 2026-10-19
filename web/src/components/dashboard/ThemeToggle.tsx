import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

type Theme = "system" | "light" | "dark";
const STORAGE_KEY = "qoe-dashboard-theme";

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Storage can be unavailable (private mode, blocked site data); fall back to the system theme.
  }
  return "system";
}

/** Applies shadcn's `.dark` class: follows the OS by default, or the viewer's explicit choice. */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && media.matches);
      document.documentElement.classList.toggle("dark", dark);
    };
    apply();
    media.addEventListener("change", apply);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Not persisting the choice is harmless.
    }
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  return (
    <ToggleGroup type="single" size="sm" variant="outline" value={theme}
      onValueChange={(v) => v && setTheme(v as Theme)} aria-label="Theme">
      <ToggleGroupItem value="system" aria-label="System theme"><Monitor /></ToggleGroupItem>
      <ToggleGroupItem value="light" aria-label="Light theme"><Sun /></ToggleGroupItem>
      <ToggleGroupItem value="dark" aria-label="Dark theme"><Moon /></ToggleGroupItem>
    </ToggleGroup>
  );
}
