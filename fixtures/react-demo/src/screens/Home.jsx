import { useState } from 'react'
import Button from '../components/Button.jsx'
import Modal from '../components/Modal.jsx'

export default function Home({ go }) {
  const [helpOpen, setHelpOpen] = useState(false)
  return (
    <section>
      <h1>Welcome</h1>
      <p>Create an account or manage your profile.</p>
      <div className="actions">
        <Button onClick={() => go('signup')}>Create account</Button>
        <Button variant="secondary" onClick={() => go('settings')}>View my profile</Button>
        <Button variant="secondary" onClick={() => setHelpOpen(true)}>Help</Button>
      </div>
      {helpOpen && (
        <Modal title="Help" onClose={() => setHelpOpen(false)}>
          <p>Questions about your account? Read the FAQ or contact support.</p>
          <p>
            <a href="#faq">Read the FAQ</a>
          </p>
          <Button variant="secondary">Contact support</Button>
        </Modal>
      )}
    </section>
  )
}
