export const PASSWORD_RULE_MESSAGE = 'Password must be at least 8 characters and include a number.'

/** Returns an error message, or null when the password is acceptable. */
export function validatePassword(password) {
  if (password.length < 8 || !/\d/.test(password)) return PASSWORD_RULE_MESSAGE
  return null
}
