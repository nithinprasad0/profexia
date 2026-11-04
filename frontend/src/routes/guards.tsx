import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import type { Role } from "../types/api";

export function ProtectedRoute() {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) return <div className="route-state">Loading Profexia…</div>;
  if (!isAuthenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

export function RoleRoute({ roles }: { roles: Role[] }) {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div className="route-state">Checking access…</div>;
  if (!user) return <Navigate to="/login" replace />;
  if (!roles.includes(user.role as Role)) return <Navigate to="/app" replace />;
  return <Outlet />;
}