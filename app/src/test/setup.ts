// Pin the test runner's timezone so calendar-date rendering is exercised
// in a non-UTC zone. `new Date('YYYY-MM-DD')` parses the bare date as UTC
// midnight per the ECMAScript spec; calling `.toLocaleDateString()` on
// that instant in any zone west of UTC renders the previous calendar day.
// Tests that depend on calendar-date rendering must run in a negative-
// offset zone to catch that shift — running under UTC would let a buggy
// formatter coincidentally return the right date.
process.env.TZ = 'America/Chicago';

import '@testing-library/jest-dom/vitest';
