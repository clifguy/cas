import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BulkActionBar } from '../BulkActionBar';

function setup(overrides: Partial<Parameters<typeof BulkActionBar>[0]> = {}) {
  const props = {
    count: 3,
    onSetLifecycle: vi.fn(),
    onUpdateMetadata: vi.fn(),
    onClear: vi.fn(),
    ...overrides,
  };
  render(<BulkActionBar {...props} />);
  return { ...props, user: userEvent.setup() };
}

describe('BulkActionBar', () => {
  it('renders count and two action buttons plus a clear affordance', () => {
    setup({ count: 3 });
    expect(screen.getByText(/3 selected/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /set lifecycle/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /update metadata/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /clear/i })).toBeInTheDocument();
  });

  it('invokes onSetLifecycle exactly once when the lifecycle button is clicked', async () => {
    const { user, onSetLifecycle } = setup();
    await user.click(screen.getByRole('button', { name: /set lifecycle/i }));
    expect(onSetLifecycle).toHaveBeenCalledTimes(1);
  });

  it('invokes onUpdateMetadata exactly once when the metadata button is clicked', async () => {
    const { user, onUpdateMetadata } = setup();
    await user.click(screen.getByRole('button', { name: /update metadata/i }));
    expect(onUpdateMetadata).toHaveBeenCalledTimes(1);
  });

  it('invokes onClear when the clear affordance is clicked', async () => {
    const { user, onClear } = setup();
    await user.click(screen.getByRole('button', { name: /clear/i }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
