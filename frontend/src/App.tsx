import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthProvider";
import { ProtectedRoute, RoleRoute } from "./routes/guards";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import { LearningDashboardPage } from "./pages/LearningDashboardPage";
import { CourseDetailPage } from "./pages/CourseDetailPage";
import "./styles.css";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/" element={<Navigate to="/app" replace />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route element={<ProtectedRoute />}>
            <Route path="/app" element={<LearningDashboardPage />} />
            <Route path="/app/courses/:courseId" element={<CourseDetailPage />} />
            <Route element={<RoleRoute roles={["teacher", "admin"]} />}>
              <Route path="/app/teach" element={<LearningDashboardPage mode="teach" />} />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
