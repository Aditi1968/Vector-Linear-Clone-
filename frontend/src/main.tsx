import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './app/App'
import './styles/index.css'

const container = document.getElementById('root')

if (container === null) {
  // Thrown rather than silently skipped: a missing mount point means
  // index.html and this file disagree, and a blank page with a clean console
  // is a much worse way to find that out.
  throw new Error('Root container #root is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
