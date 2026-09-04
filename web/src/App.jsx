import React from "react";
import { Routes, Route } from "react-router-dom";
import { AuthProvider } from "./AuthContext.jsx";
import { RequireAuth } from "./Layout.jsx";
import UserPicker from "./pages/UserPicker.jsx";
import Login from "./pages/Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import OrderBuilder from "./pages/OrderBuilder.jsx";

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/" element={<UserPicker />} />
        <Route path="/login/:userId" element={<Login />} />
        <Route
          path="/dashboard"
          element={
            <RequireAuth>
              <Dashboard />
            </RequireAuth>
          }
        />
        <Route
          path="/order"
          element={
            <RequireAuth>
              <OrderBuilder />
            </RequireAuth>
          }
        />
      </Routes>
    </AuthProvider>
  );
}
