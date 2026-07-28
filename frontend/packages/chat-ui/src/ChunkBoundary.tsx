import { Component, type ReactNode } from "react";

const RELOAD_KEY = "penny:chunk-reloaded";

/**
 * Guards a lazy route chunk. A failed dynamic import — typically a stale
 * index.html referencing a renamed hashed chunk after a redeploy — would
 * otherwise reject through Suspense and unmount the whole tree, leaving a
 * blank page. On the first failure per session we reload to fetch the fresh
 * manifest; if that already happened, render a plain reload link instead of
 * nothing.
 */
export class ChunkBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    if (!sessionStorage.getItem(RELOAD_KEY)) {
      sessionStorage.setItem(RELOAD_KEY, "1");
      window.location.reload();
    }
  }

  render() {
    if (this.state.failed) {
      return (
        <a href="/" className="m-6 inline-block font-ui text-sm text-navy underline">
          Something went wrong — reload
        </a>
      );
    }
    return this.props.children;
  }
}
