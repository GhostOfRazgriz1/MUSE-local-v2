/**
 * Browser Notifications API hook.
 *
 * Fires native desktop notifications when the tab is not visible,
 * so reminders and alerts reach the user even when they're in
 * another app.
 */

import { useState, useCallback, useEffect } from "react";

const STORAGE_KEY = "muse-notifications-enabled";

export function useNotifications() {
  const [permitted, setPermitted] = useState(false);

  // Check current permission state on mount
  useEffect(() => {
    if (!("Notification" in window)) return;
    setPermitted(Notification.permission === "granted");
  }, []);

  // Restore user preference from localStorage
  const [enabled, setEnabled] = useState(() => {
    return localStorage.getItem(STORAGE_KEY) !== "false";
  });

  const requestPermission = useCallback(async () => {
    if (!("Notification" in window)) return false;
    const result = await Notification.requestPermission();
    const granted = result === "granted";
    setPermitted(granted);
    if (granted) {
      setEnabled(true);
      localStorage.setItem(STORAGE_KEY, "true");
    }
    return granted;
  }, []);

  const toggleEnabled = useCallback((on: boolean) => {
    setEnabled(on);
    localStorage.setItem(STORAGE_KEY, on ? "true" : "false");
  }, []);

  const notify = useCallback(
    (title: string, body: string) => {
      if (!permitted || !enabled) return;
      // Only fire desktop notification when tab is not visible
      if (!document.hidden) return;

      try {
        new Notification(title, {
          body,
          icon: "/icon.png",
          tag: `muse-${Date.now()}`, // unique tag prevents stacking
        });
      } catch {
        // Notification API may fail in some contexts (e.g., insecure origin)
      }
    },
    [permitted, enabled]
  );

  return {
    /** Browser has granted notification permission */
    permitted,
    /** User has enabled notifications (can be toggled off even if permitted) */
    enabled,
    /** Request browser permission (triggers the browser prompt) */
    requestPermission,
    /** Toggle notifications on/off (doesn't revoke browser permission) */
    toggleEnabled,
    /** Fire a desktop notification (only if tab is hidden) */
    notify,
    /** Whether the browser supports notifications at all */
    supported: typeof window !== "undefined" && "Notification" in window,
  };
}
