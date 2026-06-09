import 'package:flutter/material.dart';
import 'screens/scrape_screen.dart';

void main() {
  runApp(const BinaApp());
}

class BinaApp extends StatelessWidget {
  const BinaApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'BinaHealth',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.teal),
        useMaterial3: true,
      ),
      home: const ScrapeScreen(),
    );
  }
}
