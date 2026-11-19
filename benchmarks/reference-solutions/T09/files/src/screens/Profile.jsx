import { useState } from 'react'
import Tooltip from '../components/Tooltip.jsx'
import { validatePassword } from '../lib/validation.js'

export default function Profile() {
  const [name, setName] = useState('Jane Doe')
  const [newPassword, setNewPassword] = useState('')
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(false)

  function save(e) {
    e.preventDefault()
    setSaved(false)
    const passwordError = newPassword ? validatePassword(newPassword) : null
    if (passwordError) {
      setError(passwordError)
      return
    }
    setError(null)
    setSaved(true)
  }

  return (
    <section>
      <h1>Profile</h1>
      <form className="form" onSubmit={save} noValidate>
        <label htmlFor="display-name">
          Display name <Tooltip text="Shown to other users">(?)</Tooltip>
        </label>
        <input id="display-name" value={name} onChange={(e) => setName(e.target.value)} />
        <label htmlFor="new-password">New password</label>
        <input id="new-password" type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        {saved && <p role="status">Saved.</p>}
        <div className="form-row">
          <button
            type="submit"
            style={{ background: '#1e90ff', color: '#fff', border: 'none', borderRadius: 2, padding: '6px 10px' }}
          >
            Save
          </button>
        </div>
      </form>
    </section>
  )
}
