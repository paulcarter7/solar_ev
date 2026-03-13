import "@testing-library/jest-dom";

// Recharts' ResponsiveContainer uses ResizeObserver which jsdom doesn't implement.
// Stub it out so App tests that render the full component tree don't crash.
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};
