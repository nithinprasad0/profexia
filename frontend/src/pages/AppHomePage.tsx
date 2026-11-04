import { useAuth } from "../auth/AuthProvider";
import { learningApi } from "../api/resources";
import { useEffect, useState } from "react";
import type { Course } from "../types/api";

export function AppHomePage() {
  const { user, logout } = useAuth();
  const [courses, setCourses] = useState<Course[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    void learningApi.courses().then(setCourses).catch(() => setError("Could not load courses."));
  }, []);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div><span className="brand">Profexia</span><span className="role">{user?.role}</span></div>
        <button className="secondary" onClick={() => void logout()}>Sign out</button>
      </header>
      <section className="hero">
        <p className="eyebrow">PHASE 4.1 FOUNDATION</p>
        <h1>Hello, {user?.username}.</h1>
        <p className="muted">The frontend foundation is connected to the finished Phase 3.6 API.</p>
      </section>
      <section>
        <div className="section-heading"><h2>Courses</h2><span>{courses.length}</span></div>
        {error && <div className="error">{error}</div>}
        <div className="grid">
          {courses.map((course) => (
            <article className="card" key={course.id}>
              <p className="eyebrow">COURSE #{course.id}</p>
              <h3>{course.title}</h3>
              <p className="muted">{course.description ?? "No description yet."}</p>
              <small>Teacher: {course.teacher_username}</small>
            </article>
          ))}
          {!courses.length && !error && <div className="card muted">No courses returned by the API yet.</div>}
        </div>
      </section>
    </main>
  );
}