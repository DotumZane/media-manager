const form = document.querySelector("#login-form");
const usernameField = form.elements.username;
const passwordField = form.elements.password;
const errorMessage = document.querySelector("#login-error");
const submitButton = document.querySelector("#sign-in-button");
const passwordToggle = document.querySelector("#password-toggle");

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorMessage.hidden = true;
  submitButton.disabled = true;
  submitButton.querySelector("span").textContent = "Signing in…";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: usernameField.value.trim(),
        password: passwordField.value,
      }),
      cache: "no-store",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "Sign in failed.");
    window.location.replace("/");
  } catch (error) {
    showError(error.message || "Could not sign in. Please try again.");
    submitButton.disabled = false;
    submitButton.querySelector("span").textContent = "Sign in";
    passwordField.focus();
    passwordField.select();
  }
});

passwordToggle.addEventListener("click", () => {
  const showing = passwordField.type === "text";
  passwordField.type = showing ? "password" : "text";
  passwordToggle.textContent = showing ? "Show" : "Hide";
  passwordToggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
  passwordToggle.setAttribute("aria-pressed", String(!showing));
  passwordField.focus();
});

fetch("/api/session", { cache: "no-store" })
  .then((response) => response.json())
  .then((result) => {
    if (!result.configured) {
      showError(result.message);
      submitButton.disabled = true;
    }
  })
  .catch(() => showError("Could not reach Media Manager. Refresh the page to try again."));
