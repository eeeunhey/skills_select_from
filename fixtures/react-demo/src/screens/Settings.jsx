import Button from '../components/Button.jsx'

export default function Settings({ go }) {
  return (
    <section>
      <h1>Settings</h1>
      <label className="check">
        <input type="checkbox" defaultChecked /> Email notifications
      </label>
      <div className="actions">
        <Button variant="secondary" onClick={() => go('home')}>Back to home</Button>
      </div>
    </section>
  )
}
