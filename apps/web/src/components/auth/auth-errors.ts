import { ApiError } from "@/lib/api";

export type AuthErrorContext = "login" | "register" | "verify" | "reset" | "forgot";

/**
 * Turn what the server said into plain words that also say what to do next.
 * Anything we do not recognise falls back to the server's own message.
 */
export function plainAuthError(err: unknown, context: AuthErrorContext): string {
  if (!(err instanceof ApiError)) {
    return "Something went wrong. Please try again in a minute.";
  }
  if (err.status === 0) {
    return "We could not reach ComputeCoin. Check your internet connection, then try again.";
  }
  if (err.status === 429) {
    return "Too many tries in a short time. Wait a minute, then try again.";
  }
  if (err.status >= 500) {
    return "Something went wrong on our side. Please try again in a minute.";
  }
  if (context === "login" && err.status === 401) {
    return "That email or password is not right. Check both and try again, or use “Forgot password?” to reset it.";
  }
  if (context === "register" && err.status === 409) {
    return "We could not create an account with those details. If you already have an account, log in instead. If not, try a different email.";
  }
  if (err.message === "aup_not_accepted") {
    return "You need to accept the Acceptable Use Policy to create an account. Tick the box and try again.";
  }
  if (err.message === "Invalid or expired token") {
    if (context === "verify") {
      return "This link has expired or was already used. Log in, then press “Resend email” at the top of the page to get a new one.";
    }
    return "This reset link has expired or was already used. Ask for a new link and try again.";
  }
  if (/^Request failed with status/.test(err.message)) {
    return "Something went wrong. Please try again in a minute.";
  }
  return err.message;
}
