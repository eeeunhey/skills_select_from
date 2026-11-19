import Button from '../components/Button.jsx'

export default function Success({ go }) {
  return (
    <section>
      <h1>Account created</h1>
      <p>Welcome aboard.</p>
      <Button onClick={() => go('home')}>Go home</Button>
    </section>
  )
}
