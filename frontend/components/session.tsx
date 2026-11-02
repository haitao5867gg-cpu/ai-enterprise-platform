"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  getAuthStatus,
  hasRole,
  type AuthStatus,
  type Role,
} from "../lib/auth";

// Who is using the page (docs/api/auth.md). `status` is null while it loads and when
// the API did not answer; then pages behave as before sign-in existed.
type Session = {
  status: AuthStatus | null;
  loaded: boolean;
  refresh: () => Promise<AuthStatus | null>;
};
const SessionContext = createContext<Session>({
  status: null,
  loaded: false,
  refresh: async () => null,
});

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const refresh = useCallback(async () => {
    let next: AuthStatus | null = null;
    try {
      next = await getAuthStatus();
    } catch {
      next = null;
    }
    setStatus(next);
    setLoaded(true);
    return next;
  }, []);
  useEffect(() => {
    let live = true;
    getAuthStatus()
      .then((next) => live && setStatus(next))
      .catch(() => live && setStatus(null))
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
    };
  }, []);
  return (
    <SessionContext.Provider value={{ status, loaded, refresh }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  return useContext(SessionContext);
}

// The signed-in account when sign-in is on; null when it is off (or not loaded).
export function useSignedIn() {
  const { status } = useSession();
  return status?.enabled ? status.user : null;
}

// Whether the person may act as `role`: always when sign-in is off.
export function useMay(role: Role) {
  const { status } = useSession();
  if (!status?.enabled) return true;
  return hasRole(status.user, role);
}
