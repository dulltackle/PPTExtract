import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

afterEach(() => {
  globalThis.localStorage.clear();
});
