import { api } from "./client";
import type {
  Attempt, ChatMessage, CodeExecution, Course, Enrollment, LearningSession,
  LiveKitToken, Notification, PaymentOrder, Question, Result, Skill, Teacher, Test,
} from "../types/api";

export const learningApi = {
  skills: () => api.get<Skill[]>("/skills"),
  createSkill: (body: { name: string; description?: string | null }) => api.post<Skill>("/skills", body),
  teachers: () => api.get<Teacher[]>("/teachers"),
  courses: () => api.get<Course[]>("/courses"),
  createCourse: (body: { title: string; description?: string | null; skill_ids?: number[] }) =>
    api.post<Course>("/courses", body),
  course: (id: number) => api.get<Course>(`/courses/${id}`),
  updateCourse: (id: number, body: { title?: string; description?: string | null; skill_ids?: number[] }) =>
    api.patch<Course>(`/courses/${id}`, body),
  enroll: (courseId: number) => api.post<Enrollment>(`/courses/${courseId}/enroll`),
  enrollments: () => api.get<Enrollment[]>("/enrollments/me"),
  updateProgress: (id: number, progress_percent: number) =>
    api.patch<Enrollment>(`/enrollments/${id}/progress`, { progress_percent }),
};

export const assessmentApi = {
  questions: (courseId: number) => api.get<Question[]>(`/courses/${courseId}/questions`),
  createQuestion: (courseId: number, body: unknown) => api.post<Question>(`/courses/${courseId}/questions`, body),
  updateQuestion: (id: number, body: unknown) => api.patch<Question>(`/questions/${id}`, body),
  deleteQuestion: (id: number) => api.delete<{ deleted: boolean }>(`/questions/${id}`),
  tests: (courseId: number) => api.get<Test[]>(`/courses/${courseId}/tests`),
  createTest: (courseId: number, body: unknown) => api.post<Test>(`/courses/${courseId}/tests`, body),
  updateTest: (id: number, body: unknown) => api.patch<Test>(`/tests/${id}`, body),
  deleteTest: (id: number) => api.delete<{ deleted: boolean }>(`/tests/${id}`),
  startAttempt: (testId: number) => api.post<Attempt>(`/tests/${testId}/attempts`),
  attempt: (id: number) => api.get<Attempt>(`/attempts/${id}`),
  submit: (id: number, answers: { question_id: number; selected_option: number | null }[]) =>
    api.post<Result>(`/attempts/${id}/submit`, { answers }),
  result: (id: number) => api.get<Result>(`/results/${id}`),
  testResults: (testId: number) => api.get<Result[]>(`/tests/${testId}/results`),
};

/** Chat room ids are authorized server-side: course:<id>, session:<id>, dm:<lowerUserId>:<higherUserId>. */
export const chatRooms = {
  course: (courseId: number) => `course:${courseId}`,
  session: (sessionId: number) => `session:${sessionId}`,
  direct: (userA: number, userB: number) => `dm:${Math.min(userA, userB)}:${Math.max(userA, userB)}`,
};

export const realtimeApi = {
  chatHistory: (roomId: string) => api.get<ChatMessage[]>(`/chat/${encodeURIComponent(roomId)}/messages`),
  sendMessage: (roomId: string, content: string) =>
    api.post<ChatMessage>(`/chat/${encodeURIComponent(roomId)}/messages`, { content }),
  notifications: (unreadOnly = false) =>
    api.get<Notification[]>(`/notifications?unread_only=${unreadOnly}`),
  markRead: (id: number) => api.patch<Notification>(`/notifications/${id}/read`),
  markAllRead: () => api.post<{ updated: boolean }>("/notifications/read-all"),
  sessions: (courseId?: number) =>
    api.get<LearningSession[]>(courseId ? `/learning-sessions?course_id=${courseId}` : "/learning-sessions"),
  createSession: (body: unknown) => api.post<LearningSession>("/learning-sessions", body),
  session: (id: number) => api.get<LearningSession>(`/learning-sessions/${id}`),
  updateSession: (id: number, body: unknown) => api.patch<LearningSession>(`/learning-sessions/${id}`, body),
};

export const providerApi = {
  createPaymentOrder: (body: { amount: number; currency?: string; receipt?: string; notes?: Record<string, unknown> }) =>
    api.post<PaymentOrder>("/payments/orders", body),
  executeCode: (body: { language_id: number; source_code: string; stdin?: string | null }) =>
    api.post<CodeExecution>("/code-executions", body),
  codeExecution: (id: number) => api.get<CodeExecution>(`/code-executions/${id}`),
  // The server derives the room from the session (profexia-session-<id>); room_name is optional.
  livekitToken: (sessionId: number, body: { room_name?: string; ttl_minutes?: number } = {}) =>
    api.post<LiveKitToken>(`/learning-sessions/${sessionId}/livekit-token`, body),
};