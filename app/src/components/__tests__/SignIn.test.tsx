import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SignIn from '../SignIn';
import * as authApi from '../../api/auth';

vi.mock('../../api/auth', () => ({
  beginLogin: vi.fn(),
}));

const beginLoginMock = vi.mocked(authApi.beginLogin);

const originalLocation = window.location;
const assignMock = vi.fn();

beforeEach(() => {
  beginLoginMock.mockReset();
  assignMock.mockReset();
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { assign: assignMock, href: '' } as unknown as Location,
  });
});

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: originalLocation });
});

describe('SignIn interstitial', () => {
  it('C1: renders the "Sign in with Microsoft" button', () => {
    render(<SignIn />);
    expect(screen.getByRole('button', { name: /sign in with microsoft/i })).toBeInTheDocument();
  });

  it('C2: click fetches the challenge and navigates the browser to the authorization URL', async () => {
    beginLoginMock.mockResolvedValue({
      authorization_url: 'https://login.microsoftonline.com/tenant/authorize?x=1',
      state: 's',
    });
    render(<SignIn />);

    await userEvent.click(screen.getByRole('button', { name: /sign in with microsoft/i }));

    expect(beginLoginMock).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(assignMock).toHaveBeenCalledWith('https://login.microsoftonline.com/tenant/authorize?x=1'),
    );
  });

  it('C3: a failed challenge surfaces an error and does not navigate', async () => {
    beginLoginMock.mockRejectedValue(new Error('could not reach identity provider'));
    render(<SignIn />);

    await userEvent.click(screen.getByRole('button', { name: /sign in with microsoft/i }));

    await waitFor(() =>
      expect(screen.getByText(/could not reach identity provider/i)).toBeInTheDocument(),
    );
    expect(assignMock).not.toHaveBeenCalled();
  });
});
