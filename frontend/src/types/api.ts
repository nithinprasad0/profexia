export type Role = "learner" | "teacher" | "admin";

export interface User {
  id: number;
  email: string;
  username: string;
  is_active: boolean;
  role: Role | string;
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface Skill {
  id: number;
  name: string;
  description: string | null;
}

export interface Course {
  id: number;
  title: string;
  description: string | null;
  teacher_id: number;
  teacher_username: string;
  skills: Skill[];
}

export interface Teacher {
  id: number;
  username: string;
  display_name: string | null;
  bio: string | null;
}

export interface Enrollment {
  id: number;
  course_id: number;
  learner_id: number;
  progress_percent: number;
  status: string;
}

export interface Question {
  id: number;
  course_id: number;
  teacher_id: number;
  text: string;
  options: string[];
  explanation: string | null;
  marks: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Test {
  id: number;
  course_id: number;
  teacher_id: number;
  title: string;
  description: string | null;
  time_limit_minutes: number | null;
  max_marks: number;
  is_published: boolean;
  question_ids: number[];
  created_at: string;
  updated_at: string;
}

export interface AttemptQuestion {
  id: number;
  text: string;
  options: string[];
  marks: number;
}

export interface Attempt {
  id: number;
  test_id: number;
  learner_id: number;
  started_at: string;
  submitted_at: string | null;
  status: string;
  questions: AttemptQuestion[];
}

export interface ResultAnswer {
  question_id: number;
  selected_option: number | null;
  correct: boolean;
  marks_awarded: number;
}

export interface Result {
  id: number;
  test_id: number;
  learner_id: number;
  submission_id: number;
  score: number;
  max_score: number;
  percentage: number;
  passed: boolean;
  submitted_at: string;
  answers: ResultAnswer[];
}

export interface ChatMessage {
  id: number;
  room_id: string;
  sender_id: number;
  content: string;
  created_at: string;
}

export interface Notification {
  id: number;
  recipient_id: number;
  type: string;
  title: string;
  message: string;
  data: Record<string, unknown> | null;
  is_read: boolean;
  created_at: string;
}

export interface LearningSession {
  id: number;
  course_id: number;
  host_id: number;
  title: string;
  description: string | null;
  scheduled_at: string | null;
  status: string;
  created_at: string;
}

export interface PaymentOrder {
  id: number;
  provider: string;
  provider_order_id: string;
  amount: number;
  currency: string;
  status: string;
  receipt: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface CodeExecution {
  id: number;
  provider: string;
  provider_submission_id: string | null;
  language_id: number;
  status: string;
  stdout: string | null;
  stderr: string | null;
  compile_output: string | null;
  time: string | null;
  memory: number | null;
  created_at: string;
  updated_at: string;
}

export interface LiveKitToken {
  livekit_url: string;
  room_name: string;
  identity: string;
  token: string;
  expires_in_minutes: number;
}