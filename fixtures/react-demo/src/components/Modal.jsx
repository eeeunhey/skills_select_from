import Button from './Button.jsx'

export default function Modal({ title, onClose, children }) {
  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-labelledby="modal-title">
        <h2 id="modal-title">{title}</h2>
        {children}
        <div className="modal-actions">
          <Button onClick={onClose}>Close</Button>
        </div>
      </div>
    </div>
  )
}
