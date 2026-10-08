import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import './styles.css'

const container = document.getElementById('root')
if (container === null) {
  throw new Error('Dashboard root element #root is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    {/* The v7 future flags are opt-in behaviours we already satisfy; setting them
        now keeps the console free of deprecation warnings and makes the eventual
        v7 upgrade a version bump rather than a behaviour change. */}
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
