import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { learningApi } from "../api/resources";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import type { Course, Enrollment, Skill, Teacher } from "../types/api";

type Props = { mode?: "learn" | "teach" };

function errorText(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    if (typeof error.detail === "string") return error.detail;
    if (Array.isArray(error.detail)) return "Please check the submitted fields.";
  }
  return fallback;
}

export function LearningDashboardPage({ mode = "learn" }: Props) {
  const { user, logout } = useAuth();
  const isTeachMode = mode === "teach";
  const [courses, setCourses] = useState<Course[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [teachers, setTeachers] = useState<Teacher[]>([]);
  const [enrollments, setEnrollments] = useState<Enrollment[]>([]);
  const [query, setQuery] = useState("");
  const [skillFilter, setSkillFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  const isTeacher = user?.role === "teacher" || user?.role === "admin";

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const results = await Promise.all([
        learningApi.courses(),
        learningApi.skills(),
        learningApi.teachers(),
        // /enrollments/me is learner-only (403 for teachers/admins); fetching it for
        // other roles made Promise.all reject and hid the whole workspace.
        user?.role === "learner" ? learningApi.enrollments() : Promise.resolve([] as Enrollment[]),
      ]);
      setCourses(results[0]);
      setSkills(results[1]);
      setTeachers(results[2]);
      setEnrollments(results[3]);
    } catch (err) {
      setError(errorText(err, "Unable to load the learning workspace."));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const enrolledIds = useMemo(() => new Set(enrollments.map((e) => e.course_id)), [enrollments]);
  const visibleCourses = useMemo(() => {
    const q = query.trim().toLowerCase();
    return courses.filter((course) => {
      const matchesText = !q || `${course.title} ${course.description ?? ""} ${course.teacher_username}`.toLowerCase().includes(q);
      const matchesSkill = !skillFilter || course.skills.some((skill) => String(skill.id) === skillFilter);
      return matchesText && matchesSkill;
    });
  }, [courses, query, skillFilter]);

  const myCourses = courses.filter((c) => enrolledIds.has(c.id));
  const progressByCourse = new Map(enrollments.map((e) => [e.course_id, e]));

  const enroll = async (courseId: number) => {
    try {
      const enrollment = await learningApi.enroll(courseId);
      setEnrollments((current) => [...current.filter((e) => e.course_id !== courseId), enrollment]);
    } catch (err) {
      setError(errorText(err, "Could not enroll in this course."));
    }
  };

  return (
    <main className="learning-shell">
      <header className="topbar learning-topbar">
        <Link className="brand" to="/app">Profexia</Link>
        <nav className="nav-links">
          <a href="#courses">Courses</a>
          <a href="#teachers">Teachers</a>
          {isTeacher && <Link to="/app/teach">Teach</Link>}
        </nav>
        <div className="top-actions">
          <span className="user-chip">{user?.username}<small>{user?.role}</small></span>
          <button className="secondary compact" onClick={() => void logout()}>Sign out</button>
        </div>
      </header>

      <section className="learning-hero">
        <div>
          <p className="eyebrow">{isTeachMode ? "TEACHER WORKSPACE" : "LEARNING HUB"}</p>
          <h1>{isTeachMode ? "Share skills that move others forward." : "Learn skills that move you forward."}</h1>
          <p className="muted hero-copy">{isTeachMode ? "Create courses, share your expertise, and help learners grow from one workspace." : "Discover courses, learn from teachers, enroll, and track your progress from one workspace."}</p>
          <div className="hero-stats">
            <span><strong>{courses.length}</strong> courses</span>
            <span><strong>{enrollments.length}</strong> enrolled</span>
            <span><strong>{teachers.length}</strong> teachers</span>
          </div>
        </div>
        <div className="hero-panel">
          <span className="eyebrow">YOUR LEARNING</span>
          <h3>{myCourses.length ? "Keep your momentum." : "Start your first course."}</h3>
          <p className="muted">{myCourses.length ? `${myCourses.length} course${myCourses.length === 1 ? "" : "s"} in progress.` : "Browse the catalog below and choose a course."}</p>
          {myCourses[0] && <Link className="button-link" to={`/app/courses/${myCourses[0].id}`}>Continue learning</Link>}
        </div>
      </section>

      <section className="learning-section" id="courses">
        <div className="section-heading">
          <div><p className="eyebrow">COURSE CATALOG</p><h2>Explore courses</h2></div>
          {isTeacher && <button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Close" : "Create course"}</button>}
        </div>

        {showCreate && <CreateCourseForm skills={skills} onCreated={(course) => { setCourses((c) => [course, ...c]); setShowCreate(false); }} onError={setError} />}

        <div className="filters">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search courses, teachers, topics…" />
          <select value={skillFilter} onChange={(e) => setSkillFilter(e.target.value)}>
            <option value="">All skills</option>
            {skills.map((skill) => <option value={skill.id} key={skill.id}>{skill.name}</option>)}
          </select>
        </div>

        {error && <div className="error">{error}</div>}
        {loading ? <div className="loading-card">Loading your learning workspace…</div> :
          visibleCourses.length ? <div className="course-grid">
            {visibleCourses.map((course) => <CourseCard key={course.id} course={course} enrolled={enrolledIds.has(course.id)} enrollment={progressByCourse.get(course.id)} onEnroll={user?.role === "learner" ? () => void enroll(course.id) : undefined} />)}
          </div> :
          <div className="empty-card"><h3>No courses found</h3><p className="muted">Try another search or clear the skill filter.</p></div>
        }
      </section>

      <section className="learning-section" id="teachers">
        <div className="section-heading"><div><p className="eyebrow">TEACHER DISCOVERY</p><h2>Meet the teachers</h2></div></div>
        {teachers.length ? <div className="teacher-grid">{teachers.map((teacher) =>
          <article className="teacher-card" key={teacher.id}>
            <div className="avatar">{(teacher.display_name ?? teacher.username).slice(0, 1).toUpperCase()}</div>
            <div><h3>{teacher.display_name || teacher.username}</h3><p className="muted">@{teacher.username}</p><p>{teacher.bio || "Ready to share knowledge and help learners grow."}</p></div>
          </article>
        )}</div> : <div className="empty-card"><p className="muted">No teachers are available yet.</p></div>}
      </section>
    </main>
  );
}

function CourseCard({ course, enrolled, enrollment, onEnroll }: { course: Course; enrolled: boolean; enrollment?: Enrollment; onEnroll?: () => void }) {
  return <article className="course-card">
    <div className="course-card-top"><span className="course-id">COURSE #{course.id}</span><span className={enrolled ? "status-pill active" : "status-pill"}>{enrolled ? `${enrollment?.progress_percent ?? 0}% complete` : "Available"}</span></div>
    <h3>{course.title}</h3>
    <p className="muted course-description">{course.description || "A focused learning experience on Profexia."}</p>
    <div className="skill-row">{course.skills.slice(0, 3).map((s) => <span key={s.id}>{s.name}</span>)}</div>
    <div className="course-footer"><small>By {course.teacher_username}</small><Link className="button-link secondary-link" to={`/app/courses/${course.id}`}>View course</Link>{!enrolled && onEnroll && <button onClick={onEnroll}>Enroll</button>}</div>
    {enrolled && <div className="progress-track"><span style={{ width: `${Math.min(100, Math.max(0, enrollment?.progress_percent ?? 0))}%` }} /></div>}
  </article>;
}

function CreateCourseForm({ skills, onCreated, onError }: { skills: Skill[]; onCreated: (course: Course) => void; onError: (message: string) => void }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [skillIds, setSkillIds] = useState<number[]>([]);
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return onError("Course title is required.");
    setSaving(true);
    try {
      onCreated(await learningApi.createCourse({ title: title.trim(), description: description.trim() || null, skill_ids: skillIds }));
    } catch (err) {
      onError(errorText(err, "Could not create the course."));
    } finally { setSaving(false); }
  };
  return <form className="create-panel" onSubmit={submit}>
    <div><p className="eyebrow">NEW COURSE</p><h3>Create a learning experience</h3></div>
    <label>Title<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Python for Data Analysis" /></label>
    <label>Description<textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} placeholder="What will learners gain?" /></label>
    <label>Skills<select multiple value={skillIds.map(String)} onChange={(e) => setSkillIds(Array.from(e.target.selectedOptions).map((o) => Number(o.value)))}>{skills.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
    <button disabled={saving}>{saving ? "Creating…" : "Create course"}</button>
  </form>;
}
