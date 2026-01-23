// Route definitions
import Home from './Home.svelte';
import Admin from './Admin.svelte';
import Client from './Client.svelte';
import Login from '../components/Login.svelte';

export default {
  '/': Home,
  '/admin': Admin,
  '/admin/*': Admin, // Catch-all for all admin sub-routes
  '/client': Client,
  '/login': Login,
};
