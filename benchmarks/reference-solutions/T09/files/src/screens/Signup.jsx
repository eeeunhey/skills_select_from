import { useState } from 'react'
import { signup } from '../api.js'
import { validatePassword } from '../lib/validation.js'

export default function Signup({ go }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    const passwordError = validatePassword(password)
    if (passwordError) {
      setError(passwordError)
      return
    }
    setError(null)
    console.log('signup submitted', { email })
    try {
      await signup({ email, password })
      go('success')
    } catch (err) {
      // TODO: show <Spinner /> while the request is pending
      console.error('signup failed for ' + email, err)
    }
  }

  return (
    <section>
      <h1>Create account</h1>
      <form className="form" onSubmit={handleSubmit} noValidate>
        <label htmlFor="email">Email</label>
        <input id="email" type="text" value={email} onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-row">
          <button type="submit" className="signup-submit">Sign up</button>
          <span className="hint">By signing up you agree to the terms of service.</span>
        </div>
      </form>
    </section>
  )
}
