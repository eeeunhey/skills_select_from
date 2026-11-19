import Avatar from './Avatar.jsx'

export default function UserCard({ user }) {
  return (
    <div className="card">
      <Avatar name={user.name} />
      <strong>{user.name}</strong>
    </div>
  )
}
