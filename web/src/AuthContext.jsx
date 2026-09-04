import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem("sankalp_token"));
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!token) {
      setReady(true);
      return;
    }
    api
      .session(token)
      .then(setUser)
      .catch(() => {
        localStorage.removeItem("sankalp_token");
        setToken(null);
      })
      .finally(() => setReady(true));
  }, [token]);

  const login = useCallback(async (userId, password) => {
    const { token: t, user: u } = await api.login(userId, password);
    localStorage.setItem("sankalp_token", t);
    setToken(t);
    setUser(u);
    return u;
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("sankalp_token");
    setToken(null);
    setUser(null);
  }, []);

  const refresh = useCallback(async () => {
    if (!token) return;
    setUser(await api.session(token));
  }, [token]);

  return (
    <AuthContext.Provider value={{ token, user, ready, login, logout, refresh, setUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
