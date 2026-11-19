export default function Avatar({ name }) {
  const initials = name
    .split(' ')
    .map((part) => part[0])
    .join('')
  return <span className="avatar">{initials}</span>
}
