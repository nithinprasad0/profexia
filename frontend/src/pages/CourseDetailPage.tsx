import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { learningApi } from "../api/resources";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import type { Course, Enrollment } from "../types/api";

function message(error: unknown) {
  if (error instanceof ApiError && typeof error.detail === "string") return error.detail;
  return "Something went wrong. Please try again.";
}

export function CourseDetailPage() {
  const { courseId } = useParams();
  const id = Number(courseId);
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [course, setCourse] = useState<Course | null>(null);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [progress, setProgress] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");

  const isOwner = useMemo(() => Boolean(course && user && course.teacher_id === user.id), [course, user]);
  const isTeacher = user?.role === "teacher" || user?.role === "admin";

  useEffect(() => {
    if (!Number.isFinite(id)) return;
    setLoading(true);
    // Enrollments are learner-only on the API; teachers/admins get an empty list.
    const enrollmentsRequest = user?.role === "learner" ? learningApi.enrollments() : Promise.resolve([] as Enrollment[]);
    Promise.all([learningApi.course(id), enrollmentsRequest])
      .then(([nextCourse, enrollments]) => {
        setCourse(nextCourse);
        setTitle(nextCourse.title);
        setDescription(nextCourse.description ?? "");
        const current = enrollments.find((item) => item.course_id === id) ?? null;
        setEnrollment(current);
        setProgress(current?.progress_percent ?? 0);
      })
      .catch((err) => setError(message(err)))
      .finally(() => setLoading(false));
  }, [id, user?.role]);

  const enroll = async () => {
    setSaving(true); setError("");
    try {
      const next = await learningApi.enroll(id);
      setEnrollment(next); setProgress(next.progress_percent);
    } catch (err) { setError(message(err)); }
    finally { setSaving(false); }
  };

  const saveProgress = async (event: FormEvent) => {
    event.preventDefault();
    if (!enrollment) return;
    setSaving(true); setError("");
    try {
      const next = await learningApi.updateProgress(enrollment.id, Math.round(progress));
      setEnrollment(next); setProgress(next.progress_percent);
    } catch (err) { setError(message(err)); }
    finally { setSaving(false); }
  };

  const saveCourse = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true); setError("");
    try {
      const next = await learningApi.updateCourse(id, { title: title.trim(), description: description.trim() || null });
      setCourse(next); setEditing(false);
    } catch (err) { setError(message(err)); }
    finally { setSaving(false); }
  };

  if (loading) return <div className="route-state">Loading course…</div>;
  if (!course) return <div className="route-state"><div><h2>Course unavailable</h2><Link to="/app">Back to learning hub</Link></div></div>;

  return <main className="learning-shell">
    <header className="topbar learning-topbar">
      <Link className="brand" to="/app">Profexia</Link>
      <div className="top-actions"><span className="user-chip">{user?.username}<small>{user?.role}</small></span><button className="secondary compact" onClick={() => void logout()}>Sign out</button></div>
    </header>

    <div className="breadcrumb"><Link to="/app">Learning hub</Link><span>/</span><span>{course.title}</span></div>
    {error && <div className="error">{error}</div>}

    <section className="course-hero">
      <div>
        <p className="eyebrow">COURSE #{course.id}</p>
        {editing ? <form className="edit-course" onSubmit={saveCourse}><input value={title} onChange={(e) => setTitle(e.target.value)} /><textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={4} /><div><button disabled={saving}>{saving ? "Saving…" : "Save changes"}</button><button type="button" className="secondary" onClick={() => setEditing(false)}>Cancel</button></div></form> :
          <><h1>{course.title}</h1><p className="muted large-copy">{course.description || "Build practical knowledge through this Profexia course."}</p></>}
        <div className="skill-row large">{course.skills.map((s) => <span key={s.id}>{s.name}</span>)}</div>
        <p className="teacher-line">Taught by <strong>{course.teacher_username}</strong></p>
      </div>
      <aside className="enrollment-panel">
        {enrollment ? <>
          <span className="eyebrow">YOUR PROGRESS</span>
          <strong className="progress-number">{progress}%</strong>
          <div className="progress-track large"><span style={{ width: `${progress}%` }} /></div>
          <form onSubmit={saveProgress} className="progress-form">
            <label>Update progress
              <input type="range" min="0" max="100" value={progress} onChange={(e) => setProgress(Number(e.target.value))} />
            </label>
            <button disabled={saving}>{saving ? "Saving…" : "Save progress"}</button>
          </form>
          <p className="muted">Status: {enrollment.status}</p>
        </> : user?.role !== "learner" ? <>
          <span className="eyebrow">{isOwner ? "YOUR COURSE" : "COURSE"}</span>
          <p className="muted">Only learner accounts can enroll in courses.</p>
        </> : <>
          <span className="eyebrow">READY TO LEARN?</span>
          <h3>Join this course</h3>
          <p className="muted">Enroll to start tracking your learning progress.</p>
          <button disabled={saving} onClick={() => void enroll()}>{saving ? "Enrolling…" : "Enroll now"}</button>
        </>}
        {isTeacher && isOwner && <button className="secondary full-button" onClick={() => setEditing((v) => !v)}>{editing ? "Close editor" : "Edit course"}</button>}
      </aside>
    </section>

    <section className="course-content-grid">
      <article className="content-card"><p className="eyebrow">LEARNING PATH</p><h2>Course workspace</h2><p className="muted">This course is connected to Profexia's learning API. Assessment, real-time sessions, and other learning tools can be added here as their frontend phases are completed.</p><div className="feature-list"><div><strong>Course content</strong><span>Follow your teacher's learning material.</span></div><div><strong>Progress tracking</strong><span>Keep your completion percentage up to date.</span></div><div><strong>Assessments</strong><span>Tests and results are available through the assessment API.</span></div></div></article>
      <article className="content-card"><p className="eyebrow">ABOUT THE TEACHER</p><div className="teacher-mini"><div className="avatar">{course.teacher_username.slice(0,1).toUpperCase()}</div><div><h3>{course.teacher_username}</h3><p className="muted">Course instructor</p></div></div><Link to="/app#teachers" className="button-link secondary-link">Explore teachers</Link></article>
    </section>
    <button className="back-button" onClick={() => navigate("/app")}>← Back to courses</button>
  </main>;
}
