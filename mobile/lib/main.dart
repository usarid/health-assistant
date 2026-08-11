import 'package:flutter/material.dart';

import 'portal/portal_registry.dart';
import 'screens/scrape_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await PortalRegistry.load();
  runApp(const BinaApp());
}

class BinaApp extends StatelessWidget {
  const BinaApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'BinaHealth',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal),
        useMaterial3: true,
      ),
      home: const ScrapeScreen(),
    );
  }
}
