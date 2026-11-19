import { useState } from 'react'
import Button from './components/Button.jsx'
import Home from './screens/Home.jsx'
import Signup from './screens/Signup.jsx'
import Profile from './screens/Profile.jsx'
import Settings from './screens/Settings.jsx'
import Success from './screens/Success.jsx'

const SCREENS = { home: Home, signup: Signup, profile: Profile, settings: Settings, success: Success }

export default function App() {
  const [screen, setScreen] = useState('home')
  const Screen = SCREENS[screen]
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Acme Demo</span>
        <nav className="topnav" aria-label="Main">
          <Button variant="secondary" onClick={() => setScreen('home')}>Home</Button>
          <Button variant="secondary" onClick={() => setScreen('signup')}>Sign up</Button>
          <Button variant="secondary" onClick={() => setScreen('profile')}>Profile</Button>
          <Button variant="secondary" onClick={() => setScreen('settings')}>Settings</Button>
        </nav>
      </header>
      <main className="content">
        <Screen go={setScreen} />
      </main>
    </div>
  )
}
