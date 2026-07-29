// Route definitions
import Home from './Home.svelte';
import Admin from './Admin.svelte';
import Client from './Client.svelte';
import Reseller from './Reseller.svelte';
import Login from '../components/Login.svelte';
import VncLaunch from './VncLaunch.svelte';

export default {
  '/': Home,
  '/admin': Admin,
  '/admin/*': Admin, // Catch-all for all admin sub-routes
  '/client': Client,
  '/client/*': Client, // Catch-all for client portal sub-routes (services, detail)
  '/reseller': Reseller,
  '/reseller/*': Reseller,
  '/login': Login,
  // Standalone popup page for one-click VNC console launches (WHMCS, or a
  // direct link); redeems a ?t= launch ticket and renders the viewer
  // full-viewport with no admin/client chrome.
  '/vnc': VncLaunch,
};
